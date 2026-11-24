"""LLM retry policy shared by provider and structured-output pipelines.

The limits here intentionally stay small: callers may retry transient transport
or empty-response failures, but no path should loop indefinitely or spend user
tokens without a bounded budget.
"""

# 每层重试循环含首次调用在内的尝试上限；外层解析重试仍有独立预算。
LLM_MAX_TOTAL_ATTEMPTS = 3

_RETRYABLE_MARKERS = (
    "api returned empty content",
    "returned empty content",
    "empty non-stream content",
    "empty content",
    "empty response",
    "overloaded_error",
    "rate limit",
    "timeout",
    "temporar",
    "connection reset",
    "connection error",
    "service unavailable",
)

_RETRYABLE_STATUSES = (" 408", " 409", " 425", " 429", " 500", " 502", " 503", " 504", " 529")
_RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_RETRYABLE_ERROR_TYPES = (
    "timeout", "connectionerror", "connecterror", "readerror",
    "remoteprotocolerror", "ratelimit",
)


def is_retryable_llm_error(exc: Exception | str) -> bool:
    """Return True for transient LLM/provider failures worth retrying.

    This deliberately includes empty upstream content. Several OpenAI-compatible
    gateways occasionally return a syntactically successful response with no
    text; treating that as retryable keeps module-specific callers from each
    inventing their own empty-response handling.
    """
    causes: list[Exception] = []
    current = exc
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        causes.append(current)
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            # Typed status wins over message snippets (e.g. a 401 mentioning
            # a previous 429 is still a credential failure), including when a
            # provider has wrapped the SDK error with explicit exception chaining.
            return status in _RETRYABLE_STATUS_CODES
        current = current.__cause__
    message = " ".join(str(cause) for cause in causes).lower() if causes else str(exc).lower()
    error_types = [type(cause).__name__.lower() for cause in causes]
    return (
        any(marker in message for marker in _RETRYABLE_MARKERS)
        or any(marker in error_type for marker in _RETRYABLE_ERROR_TYPES for error_type in error_types)
        or any(status in message for status in _RETRYABLE_STATUSES)
    )
