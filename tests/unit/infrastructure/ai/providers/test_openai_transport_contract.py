"""Exercise the real SDK over an in-memory HTTP transport, with no live tokens."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.providers.openai_provider import OpenAIProvider


pytestmark = pytest.mark.asyncio
PROMPT = Prompt(system="Return the requested output.", user="Return a JSON object.")


def _response(text="OK", *, status="completed"):
    return {
        "id": "resp_test", "object": "response", "status": status, "model": "test-model",
        "output": [{
            "type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
    }


def _chat(text="OK"):
    return {
        "id": "chat_test", "object": "chat.completion", "model": "test-model", "created": 0,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def _delta(text="prefix"):
    return {
        "type": "response.output_text.delta", "delta": text, "sequence_number": 1,
        "item_id": "msg_test", "output_index": 0, "content_index": 0,
    }


def _chat_chunk(text=None, *, finish_reason=None, refusal=None):
    return {
        "id": "chat_test", "object": "chat.completion.chunk", "model": "test-model", "created": 0,
        "choices": [{"index": 0, "delta": {"content": text, "refusal": refusal}, "finish_reason": finish_reason}],
    }


def _sse(event):
    return ("data: " + json.dumps(event) + "\n\n").encode()


class _TrackedStream(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            if isinstance(chunk, asyncio.Event):
                self.waiting.set()
                await chunk.wait()
            else:
                yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
async def make_provider():
    providers = []
    client_type = httpx.AsyncClient

    def build(handler, **overrides):
        class TransportClient(client_type):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(handler), **kwargs)

        settings = Settings(
            api_key="test-key", base_url="https://gateway.example/v1", default_model="test-model",
            **overrides,
        )
        with patch("infrastructure.ai.providers.openai_provider.httpx.AsyncClient", TransportClient):
            provider = OpenAIProvider(settings)
        providers.append(provider)
        return provider

    yield build
    for provider in providers:
        await provider.aclose()


async def test_responses_wire_preserves_budget_schema_extensions_and_layered_timeout(make_provider):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=_response('{"ok":true}'))

    provider = make_provider(
        handle, extra_headers={"X-Test": "profile"}, extra_query={"region": "test"},
        extra_body={"gateway_feature": {"enabled": True}},
        connect_timeout=2, read_timeout=9, write_timeout=3, pool_timeout=1,
    )
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    fmt = {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "description": "Probe", "schema": schema}}

    result = await provider.generate(PROMPT, GenerationConfig(max_tokens=64, response_format=fmt))

    assert result.content == '{"ok":true}'
    request, = requests
    body = json.loads(request.content)
    assert body["max_output_tokens"] == 64
    assert body["text"]["format"] == {"type": "json_schema", **fmt["json_schema"]}
    assert "response_format" not in body
    assert body["gateway_feature"] == {"enabled": True}
    assert request.headers["X-Test"] == "profile"
    assert request.url.params["region"] == "test"
    assert request.extensions["timeout"] == {"connect": 2, "read": 9, "write": 3, "pool": 1}
    assert result.token_usage.input_tokens == 5


@pytest.mark.parametrize(
    "status, message",
    [(400, "Invalid temperature"), (404, "Model does not exist"), (401, "Account invalid"),
     (400, "Invalid schema for response_format")],
)
async def test_request_errors_never_switch_or_poison_responses(make_provider, status, message):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(status, json={"error": {"message": message, "type": "invalid_request_error"}})
        return httpx.Response(200, json=_response())

    provider = make_provider(handle)
    with pytest.raises(RuntimeError):
        await provider.generate(PROMPT, GenerationConfig(max_tokens=64))
    assert (await provider.generate(PROMPT, GenerationConfig(max_tokens=64))).content == "OK"
    assert paths == ["/v1/responses", "/v1/responses"]


async def test_missing_responses_endpoint_falls_back_only_for_that_profile_and_model(make_provider):
    paths = []

    def handle(request):
        body = json.loads(request.content)
        paths.append((request.url.path, body["model"]))
        if request.url.path.endswith("chat/completions"):
            return httpx.Response(200, json=_chat())
        if body["model"] == "test-model":
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        return httpx.Response(200, json=_response())

    provider = make_provider(handle)
    config = GenerationConfig(max_tokens=64)
    await provider.generate(PROMPT, config)
    await provider.generate(PROMPT, config)
    await provider.generate(PROMPT, GenerationConfig(model="other-model", max_tokens=64))
    other_profile = make_provider(lambda req: (paths.append((req.url.path, "new-profile")) or httpx.Response(200, json=_response())))
    await other_profile.generate(PROMPT, config)

    assert paths == [
        ("/v1/responses", "test-model"), ("/v1/chat/completions", "test-model"),
        ("/v1/chat/completions", "test-model"), ("/v1/responses", "other-model"),
        ("/v1/responses", "new-profile"),
    ]


@pytest.mark.parametrize("terminal", ["response.incomplete", "response.failed", "error", "disconnect"])
async def test_partial_stream_failure_is_reported_closed_and_never_replayed(make_provider, terminal):
    tail = []
    if terminal != "disconnect":
        response = _response(status=terminal.removeprefix("response."))
        response["incomplete_details"] = {"reason": "max_output_tokens"}
        tail.append(_sse({"type": terminal, "response": response}))
    upstream = _TrackedStream(_sse(_delta()), *tail)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)

    provider = make_provider(handle)
    chunks = []
    with pytest.raises(RuntimeError, match="did not complete|before response.completed"):
        async for chunk in provider.stream_generate(PROMPT, GenerationConfig(max_tokens=64)):
            chunks.append(chunk)

    assert chunks == ["prefix"]
    assert len(requests) == 1
    assert upstream.closed


@pytest.mark.parametrize("with_deltas", [False, True])
async def test_completion_snapshot_is_used_once_and_closes_without_waiting_for_eof(make_provider, with_deltas):
    chunks = [_sse(_delta("OK"))] if with_deltas else []
    upstream = _TrackedStream(
        *chunks, _sse({"type": "response.completed", "response": _response()}),
        RuntimeError("must not read beyond terminal event"),
    )
    provider = make_provider(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream))

    text = "".join([chunk async for chunk in provider.stream_generate(PROMPT, GenerationConfig(max_tokens=64))])

    assert text == "OK"
    assert upstream.closed


async def test_stream_cancellation_closes_only_the_request_and_keeps_provider_usable(make_provider):
    gate = asyncio.Event()
    upstream = _TrackedStream(_sse(_delta()), gate)
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)
        return httpx.Response(200, json=_response("next request"))

    provider = make_provider(handle)
    stream = provider.stream_generate(PROMPT, GenerationConfig(max_tokens=64))
    assert await anext(stream) == "prefix"
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(upstream.waiting.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert upstream.closed
    assert not provider._http_client.is_closed
    assert (await provider.generate(PROMPT, GenerationConfig(max_tokens=64))).content == "next request"


async def test_early_consumer_close_releases_upstream_stream(make_provider):
    upstream = _TrackedStream(_sse(_delta()), asyncio.Event())
    provider = make_provider(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream))
    stream = provider.stream_generate(PROMPT, GenerationConfig(max_tokens=64))
    assert await anext(stream) == "prefix"
    await stream.aclose()
    assert upstream.closed


async def test_incomplete_non_stream_response_with_text_is_not_success(make_provider):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        body = _response("partial output", status="incomplete")
        body["incomplete_details"] = {"reason": "max_output_tokens"}
        return httpx.Response(200, json=body)

    provider = make_provider(handle)
    with pytest.raises(RuntimeError, match="max_output_tokens"):
        await provider.generate(PROMPT, GenerationConfig(max_tokens=64))
    assert count == 1


@pytest.mark.parametrize("failure", ["status", "timeout"])
async def test_transient_failures_use_three_transport_attempts_without_sdk_multiplier(make_provider, failure):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(503, json={"error": {"message": "Temporarily unavailable"}})

    provider = make_provider(handle)
    with patch("infrastructure.ai.providers.openai_provider.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="APITimeoutError|InternalServerError"):
            await provider.generate(PROMPT, GenerationConfig(max_tokens=64))
    assert count == 3


@pytest.mark.parametrize("message, expected_requests", [
    ("Invalid schema for response_format", 1),
    ("response_format json_schema is not supported by this model", 2),
])
async def test_schema_downgrade_requires_explicit_unsupported_format(make_provider, message, expected_requests):
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": {"message": message}})
        return httpx.Response(200, json=_chat('{"ok":true}'))

    provider = make_provider(handle, use_legacy_chat_completions=True)
    config = GenerationConfig(max_tokens=64, response_format={"type": "json_schema", "json_schema": {"name": "test", "schema": {"type": "object"}}})
    if expected_requests == 1:
        with pytest.raises(RuntimeError):
            await provider.generate(PROMPT, config)
    else:
        await provider.generate(PROMPT, config)
        assert bodies[1]["response_format"] == {"type": "json_object"}
    assert len(bodies) == expected_requests


async def test_chat_stream_closes_at_finish_without_waiting_for_eof(make_provider):
    upstream = _TrackedStream(
        _sse(_chat_chunk("OK")), _sse(_chat_chunk(finish_reason="stop")),
        RuntimeError("must not read beyond finish_reason"),
    )
    provider = make_provider(
        lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream),
        use_legacy_chat_completions=True,
    )

    result = [chunk async for chunk in provider.stream_generate(PROMPT, GenerationConfig(max_tokens=64))]

    assert result == ["OK"]
    assert upstream.closed


@pytest.mark.parametrize("terminal", ["length", "content_filter", "refusal", "disconnect"])
async def test_chat_partial_stream_failure_is_reported_and_closed(make_provider, terminal):
    tail = []
    if terminal != "disconnect":
        tail = [_sse(_chat_chunk(refusal="refused"))] if terminal == "refusal" else [_sse(_chat_chunk(finish_reason=terminal))]
    upstream = _TrackedStream(_sse(_chat_chunk("prefix")), *tail)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)

    provider = make_provider(handle, use_legacy_chat_completions=True)
    chunks = []
    with pytest.raises(RuntimeError, match="did not complete|refused|before finish_reason"):
        async for chunk in provider.stream_generate(PROMPT, GenerationConfig(max_tokens=64)):
            chunks.append(chunk)

    assert chunks == ["prefix"]
    assert len(requests) == 1
    assert upstream.closed


async def test_empty_chat_aggregation_closes_at_finish_without_waiting_for_eof(make_provider):
    upstream = _TrackedStream(
        _sse(_chat_chunk("OK")), _sse(_chat_chunk(finish_reason="stop")),
        RuntimeError("must not read beyond finish_reason"),
    )
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=_chat(""))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)

    provider = make_provider(handle, use_legacy_chat_completions=True)
    result = await provider.generate(PROMPT, GenerationConfig(max_tokens=64))

    assert result.content == "OK"
    assert len(requests) == 2
    assert requests[1]["stream"] is True
    assert upstream.closed


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
async def test_partial_non_stream_chat_is_not_success_or_replayed(make_provider, finish_reason):
    requests = []

    def handle(request):
        requests.append(request)
        body = _chat("partial")
        body["choices"][0]["finish_reason"] = finish_reason
        return httpx.Response(200, json=body)

    provider = make_provider(handle, use_legacy_chat_completions=True)
    with pytest.raises(RuntimeError, match=finish_reason):
        await provider.generate(PROMPT, GenerationConfig(max_tokens=64))
    assert len(requests) == 1


async def test_refusal_in_responses_output_is_not_hidden_by_aggregate_text(make_provider):
    body = _response("partial")
    body["output"][0]["content"].append({"type": "refusal", "refusal": "Cannot comply"})
    provider = make_provider(lambda req: httpx.Response(200, json=body))
    with pytest.raises(RuntimeError, match="refused"):
        await provider.generate(PROMPT, GenerationConfig(max_tokens=64))


async def test_non_stream_endpoint_fallback_does_not_disable_responses_stream(make_provider):
    paths = []
    upstream = _TrackedStream(_sse({"type": "response.completed", "response": _response("stream")}))

    def handle(request):
        paths.append(request.url.path)
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)
        if request.url.path.endswith("/responses"):
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        return httpx.Response(200, json=_chat())

    provider = make_provider(handle)
    await provider.generate(PROMPT, GenerationConfig(max_tokens=64))
    assert [chunk async for chunk in provider.stream_generate(PROMPT, GenerationConfig(max_tokens=64))] == ["stream"]
    assert paths == ["/v1/responses", "/v1/chat/completions", "/v1/responses"]


async def test_stream_completion_refusal_is_not_hidden_by_prior_deltas(make_provider):
    body = _response("prefix")
    body["output"][0]["content"].append({"type": "refusal", "refusal": "Cannot comply"})
    upstream = _TrackedStream(_sse(_delta()), _sse({"type": "response.completed", "response": body}))
    provider = make_provider(
        lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream),
    )
    chunks = []
    with pytest.raises(RuntimeError, match="refused"):
        async for chunk in provider.stream_generate(PROMPT, GenerationConfig(max_tokens=64)):
            chunks.append(chunk)
    assert chunks == ["prefix"]
    assert upstream.closed


async def test_stream_cleanup_error_preserves_original_failure(make_provider):
    class FailingCloseStream(_TrackedStream):
        async def aclose(self):
            self.closed = True
            raise OSError("cleanup failed")

    body = _response(status="incomplete")
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    upstream = FailingCloseStream(
        _sse(_delta()), _sse({"type": "response.incomplete", "response": body}),
    )
    provider = make_provider(
        lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream),
    )
    with pytest.raises(RuntimeError, match="max_output_tokens"):
        async for _ in provider.stream_generate(PROMPT, GenerationConfig(max_tokens=64)):
            pass
    assert upstream.closed
