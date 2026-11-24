"""Continuation handlers for accepted AI Invocation outputs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from application.ai_invocation.dtos import AdoptionDecision, InvocationSession


@dataclass(frozen=True)
class ContinuationContext:
    session: InvocationSession
    decision: AdoptionDecision
    commit_id: str = ""

    @property
    def idempotency_key(self) -> str:
        """Stable key for a handler's own effect deduplication or compensation."""
        return f"{self.session.id}:{self.decision.id}:continuation"


class CommitInProgress(RuntimeError):
    """Another worker still owns execution of this decision."""


class UnsafeContinuationRetry(RuntimeError):
    """An interrupted effect needs explicit compensation before replay."""


ContinuationHandler = Callable[[ContinuationContext], Mapping[str, Any]]


class ContinuationRegistry:
    """Small registry that keeps business continuation logic out of routes."""

    def __init__(self):
        self._handlers: dict[str, ContinuationHandler] = {}
        self._retry_safe: set[str] = set()

    def register(self, key: str, handler: ContinuationHandler, *, retry_safe: bool = False) -> None:
        if not key:
            raise ValueError("continuation handler key is required")
        self._handlers[key] = handler
        self._retry_safe.discard(key)
        if retry_safe:
            self._retry_safe.add(key)

    def assert_retry_safe(self, context: ContinuationContext) -> None:
        key = context.session.continuation.handler_key if context.session.continuation else ""
        if key and key not in self._retry_safe:
            raise UnsafeContinuationRetry(
                f"continuation {key} was interrupted; compensate its effects before retry: {context.idempotency_key}"
            )

    def execute(self, context: ContinuationContext) -> dict[str, Any]:
        key = context.session.continuation.handler_key if context.session.continuation else ""
        if not key:
            return {}
        handler = self._handlers.get(key)
        if handler is None:
            raise KeyError(f"continuation handler not registered: {key}")
        return dict(handler(context) or {})


_registry = ContinuationRegistry()


def register_continuation_handler(key: str, handler: ContinuationHandler, *, retry_safe: bool = False) -> None:
    """Declare retry safety only for pure handlers or effects deduplicated by key.

    Durable step checkpoints cannot close the effect/checkpoint crash window.
    An undeclared handler is blocked on recovery until manually compensated.
    """
    _registry.register(key, handler, retry_safe=retry_safe)


def assert_continuation_retry_safe(context: ContinuationContext) -> None:
    _registry.assert_retry_safe(context)


def execute_continuation(context: ContinuationContext) -> dict[str, Any]:
    return _registry.execute(context)
