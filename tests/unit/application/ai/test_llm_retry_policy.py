import httpx
import openai
import pytest

from application.ai.llm_retry_policy import is_retryable_llm_error


@pytest.mark.parametrize("status, expected", [
    (400, False), (401, False), (403, False), (404, False), (422, False),
    (408, True), (409, True), (425, True), (429, True),
    (500, True), (502, True), (503, True), (504, True), (529, True),
])
def test_typed_http_status_takes_precedence_over_message(status, expected):
    response = httpx.Response(status, request=httpx.Request("POST", "https://gateway.example/v1/responses"))
    error = openai.APIStatusError("Previous request timed out with 429", response=response, body=None)
    assert is_retryable_llm_error(error) is expected

    wrapper = RuntimeError("Provider failed: previous request timed out with 429")
    wrapper.__cause__ = error
    assert is_retryable_llm_error(wrapper) is expected


@pytest.mark.parametrize("error_type", [
    TimeoutError, ConnectionError, httpx.ConnectError, httpx.ReadTimeout,
    httpx.ReadError, httpx.RemoteProtocolError,
])
def test_transport_errors_with_empty_messages_are_retryable(error_type):
    error = error_type("")
    assert is_retryable_llm_error(error)
    wrapper = RuntimeError("")
    wrapper.__cause__ = error
    assert is_retryable_llm_error(wrapper)


@pytest.mark.parametrize("message, expected", [
    ("API returned empty content", True),
    ("upstream returned 503", True),
    ("overloaded_error", True),
    ("invalid schema", False),
    ("max_output_tokens exceeded", False),
])
def test_legacy_string_errors(message, expected):
    assert is_retryable_llm_error(message) is expected
