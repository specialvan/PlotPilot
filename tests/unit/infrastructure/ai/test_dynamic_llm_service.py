from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from application.ai.llm_control_service import LLMProfile
from domain.ai.services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS, GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.ai.provider_factory import DynamicLLMService, _make_cache_key


PROMPT = Prompt(system="system", user="user")
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_persisted_trace(monkeypatch):
    monkeypatch.setattr("infrastructure.ai.provider_factory.get_trace_recorder", lambda: Mock())


class _FakeControlService:
    def __init__(self, profiles):
        self.profiles = profiles
        self.index = 0
        self.roles = {}

    def resolve_active_profile(self):
        return self.profiles[self.index]

    def resolve_role_profile(self, role):
        return self.roles.get(role, self.resolve_active_profile())


class _FakeProvider:
    def __init__(self, profile):
        self.name = profile.id
        self.settings = SimpleNamespace(
            default_model=profile.model, default_max_tokens=profile.max_tokens,
            default_temperature=profile.temperature, provider_name=profile.name,
        )
        self.closed = False
        self.close_count = 0
        self.generate_gate = None
        self.close_gate = None
        self.generate_entered = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.configs = []
        self.events = []

    async def generate(self, prompt, config):
        self.configs.append(config)
        self.generate_entered.set()
        if self.generate_gate is not None:
            await self.generate_gate.wait()
        assert not self.closed, "provider closed while generating"
        return GenerationResult(self.name, TokenUsage(input_tokens=1, output_tokens=1))

    async def stream_generate(self, prompt, config):
        self.configs.append(config)
        try:
            assert not self.closed
            yield f"{self.name}:prefix"
            if self.generate_gate is not None:
                await self.generate_gate.wait()
            assert not self.closed, "provider closed while streaming"
            yield f"{self.name}:suffix"
        finally:
            self.events.append("stream_close")

    async def aclose(self):
        self.close_entered.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True
        self.close_count += 1
        self.events.append("provider_close")


class _FakeFactory:
    def __init__(self, profiles):
        self.control_service = _FakeControlService(profiles)
        self.providers = []
        self.created = asyncio.Queue()
        self.configure = lambda provider: None

    def create_from_profile(self, profile):
        provider = _FakeProvider(profile)
        self.configure(provider)
        self.providers.append(provider)
        self.created.put_nowait(provider)
        return provider


def _profile(profile_id="p1", *, model="model-a", **overrides):
    return LLMProfile(id=profile_id, name=profile_id, api_key="test-key", model=model, **overrides)


async def test_reuses_idle_provider_and_closes_it_after_profile_change():
    factory = _FakeFactory([_profile(), _profile("p2", model="model-b")])
    service = DynamicLLMService(factory)
    await service.generate(PROMPT, GenerationConfig())
    await service.generate(PROMPT, GenerationConfig())
    assert len(factory.providers) == 1

    factory.control_service.index = 1
    result = await service.generate(PROMPT, GenerationConfig())

    assert result.content == "p2"
    assert len(factory.providers) == 2
    assert factory.providers[0].close_count == 1
    assert not factory.providers[1].closed
    await service.aclose()


async def test_role_switch_reuses_independent_clients():
    factory = _FakeFactory([_profile()])
    factory.control_service.roles["cheap"] = _profile("cheap", model="cheap-model")
    service = DynamicLLMService(factory)

    await service.generate(PROMPT, GenerationConfig())
    cheap = await service.generate(PROMPT, GenerationConfig(model_role="cheap"))
    main = await service.generate(PROMPT, GenerationConfig())

    assert cheap.content == "cheap"
    assert main.content == "p1"
    assert len(factory.providers) == 2
    assert all(not provider.closed for provider in factory.providers)
    await service.aclose()
    assert all(provider.close_count == 1 for provider in factory.providers)


@pytest.mark.parametrize("field, value", [
    ("api_key", "same-key-new-credential"),
    ("extra_headers", {"X-Route": "new"}),
    ("extra_query", {"region": "new"}),
    ("extra_body", {"feature": {"enabled": True}}),
])
async def test_complete_credential_and_extensions_invalidate_cache(field, value):
    profile = _profile().model_copy(update={"api_key": "same-key-old-credential"})
    factory = _FakeFactory([profile])
    service = DynamicLLMService(factory)
    await service.generate(PROMPT, GenerationConfig())
    factory.control_service.profiles[0] = profile.model_copy(update={field: value})

    await service.generate(PROMPT, GenerationConfig())

    assert len(factory.providers) == 2
    assert factory.providers[0].close_count == 1
    assert not factory.providers[1].closed
    await service.aclose()


async def test_cache_fingerprint_contains_no_raw_credentials_and_ignores_mapping_order():
    profile = _profile(extra_headers={"X-A": "a", "X-B": "b"})
    reordered = profile.model_copy(update={"extra_headers": {"X-B": "b", "X-A": "a"}})
    assert profile.api_key not in _make_cache_key(profile)
    assert _make_cache_key(profile) == _make_cache_key(reordered)


@pytest.mark.parametrize("explicit", [False, True])
async def test_profile_defaults_do_not_overwrite_explicit_default_values(explicit):
    factory = _FakeFactory([_profile(max_tokens=512, temperature=0.2)])
    service = DynamicLLMService(factory)
    config = GenerationConfig(max_tokens=DEFAULT_MAX_OUTPUT_TOKENS, temperature=1.0) if explicit else GenerationConfig()

    await service.generate(PROMPT, config)

    effective = factory.providers[0].configs[0]
    assert effective.max_tokens == (DEFAULT_MAX_OUTPUT_TOKENS if explicit else 512)
    assert effective.temperature == (1.0 if explicit else 0.2)
    await service.aclose()


@pytest.mark.parametrize("streaming", [False, True])
async def test_llm_client_inherits_dynamic_profile_defaults_when_arguments_are_omitted(streaming):
    from infrastructure.ai.llm_client import LLMClient

    factory = _FakeFactory([_profile(max_tokens=512, temperature=0.2)])
    service = DynamicLLMService(factory)
    client = LLMClient(provider=service)
    try:
        if streaming:
            assert [chunk async for chunk in client.stream_generate("user")] == ["p1:prefix", "p1:suffix"]
        else:
            assert await client.generate("user") == "p1"
        effective = factory.providers[0].configs[0]
        assert effective.max_tokens == 512
        assert effective.temperature == 0.2
    finally:
        await service.aclose()


async def test_profile_switch_defers_close_until_inflight_generation_finishes():
    gate = asyncio.Event()
    factory = _FakeFactory([_profile(), _profile("p2", model="model-b")])
    factory.configure = lambda provider: setattr(provider, "generate_gate", gate) if provider.name == "p1" else None
    service = DynamicLLMService(factory)
    first_task = asyncio.create_task(service.generate(PROMPT, GenerationConfig()))
    first = await asyncio.wait_for(factory.created.get(), timeout=1)
    await asyncio.wait_for(first.generate_entered.wait(), timeout=1)

    factory.control_service.index = 1
    second = await service.generate(PROMPT, GenerationConfig())
    assert second.content == "p2"
    assert not first.closed

    gate.set()
    assert (await first_task).content == "p1"
    assert first.close_count == 1
    assert not factory.providers[1].closed
    await service.aclose()


async def test_concurrent_replacement_creates_only_one_new_client_while_old_close_waits():
    factory = _FakeFactory([_profile(), _profile("p2", model="model-b")])
    service = DynamicLLMService(factory)
    await service.generate(PROMPT, GenerationConfig())
    old = factory.providers[0]
    old.close_gate = asyncio.Event()
    factory.control_service.index = 1

    replacing = asyncio.create_task(service.generate(PROMPT, GenerationConfig()))
    await asyncio.wait_for(old.close_entered.wait(), timeout=1)
    concurrent = await asyncio.wait_for(service.generate(PROMPT, GenerationConfig()), timeout=1)
    assert concurrent.content == "p2"
    assert len(factory.providers) == 2

    old.close_gate.set()
    assert (await replacing).content == "p2"
    assert old.close_count == 1
    await service.aclose()


async def test_early_stream_close_finalizes_upstream_before_releasing_retired_provider():
    factory = _FakeFactory([_profile(), _profile("p2", model="model-b")])
    service = DynamicLLMService(factory)
    stream = service.stream_generate(PROMPT, GenerationConfig())
    assert await anext(stream) == "p1:prefix"
    old = factory.providers[0]

    factory.control_service.index = 1
    await service.generate(PROMPT, GenerationConfig())
    assert not old.closed
    await stream.aclose()

    assert old.events == ["stream_close", "provider_close"]
    assert old.close_count == 1
    await service.aclose()


async def test_llm_client_early_stream_close_releases_retired_provider_immediately():
    from infrastructure.ai.llm_client import LLMClient

    factory = _FakeFactory([_profile(), _profile("p2", model="model-b")])
    service = DynamicLLMService(factory)
    client = LLMClient(provider=service)
    stream = client.stream_generate("user")
    try:
        assert await anext(stream) == "p1:prefix"
        old = factory.providers[0]
        factory.control_service.index = 1
        await client.generate("user")
        assert not old.closed
        await stream.aclose()
        assert old.events == ["stream_close", "provider_close"]
        assert old.close_count == 1
    finally:
        await stream.aclose()
        await service.aclose()


async def test_cancellation_releases_retired_provider_without_closing_current_one():
    factory = _FakeFactory([_profile(), _profile("p2", model="model-b")])
    gate = asyncio.Event()
    factory.configure = lambda provider: setattr(provider, "generate_gate", gate) if provider.name == "p1" else None
    service = DynamicLLMService(factory)
    task = asyncio.create_task(service.generate(PROMPT, GenerationConfig()))
    old = await asyncio.wait_for(factory.created.get(), timeout=1)
    await asyncio.wait_for(old.generate_entered.wait(), timeout=1)
    factory.control_service.index = 1
    await service.generate(PROMPT, GenerationConfig())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert old.close_count == 1
    assert not factory.providers[1].closed
    await service.aclose()


async def test_shutdown_rejects_new_work_and_closes_active_stream_on_release():
    factory = _FakeFactory([_profile()])
    service = DynamicLLMService(factory)
    stream = service.stream_generate(PROMPT, GenerationConfig())
    assert await anext(stream) == "p1:prefix"
    provider = factory.providers[0]

    await service.aclose()
    await service.aclose()
    assert not provider.closed
    with pytest.raises(RuntimeError, match="closed"):
        await service.generate(PROMPT, GenerationConfig())

    assert await anext(stream) == "p1:suffix"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert provider.events == ["stream_close", "provider_close"]
    assert provider.close_count == 1


async def test_failed_profile_replacement_keeps_previous_client_until_recovered(monkeypatch):
    factory = _FakeFactory([_profile(), _profile("p2", model="model-b")])
    service = DynamicLLMService(factory)
    await service.generate(PROMPT, GenerationConfig())
    old = factory.providers[0]
    factory.control_service.index = 1
    with monkeypatch.context() as patcher:
        patcher.setattr(factory, "create_from_profile", Mock(side_effect=ValueError("invalid profile")))
        with pytest.raises(ValueError, match="invalid profile"):
            await service.generate(PROMPT, GenerationConfig())
    assert not old.closed
    factory.control_service.index = 0
    assert (await service.generate(PROMPT, GenerationConfig())).content == "p1"
    assert len(factory.providers) == 1
    await service.aclose()


async def test_shutdown_fallback_closes_clients_for_provider_without_aclose(monkeypatch):
    factory = _FakeFactory([_profile()])
    sync_client = SimpleNamespace(is_closed=False)
    sync_client.close = lambda: setattr(sync_client, "is_closed", True)
    async_client = SimpleNamespace(closed=False)

    async def close_async():
        async_client.closed = True

    async_client.aclose = close_async
    provider = SimpleNamespace(_http_client_sync=sync_client, _http_client=async_client)
    monkeypatch.setattr(factory, "create_from_profile", lambda _: provider)
    service = DynamicLLMService(factory)
    async with service._lease_provider(GenerationConfig()) as leased:
        assert leased is provider
    await service.aclose()
    assert sync_client.is_closed
    assert async_client.closed
