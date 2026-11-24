"""SQLite single-writer dispatch. Explicit startup/migration exceptions are audited."""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from collections import Counter
import asyncio
import logging
import re
import threading
from typing import Any, Callable, List, Optional, Tuple, TypeVar, Union

from infrastructure.persistence.database.write_environment import (
    SQLiteWriteEnvironmentSettings,
)

logger = logging.getLogger(__name__)

_sqlite_writer_thread_ident: Optional[int] = None

# interfaces.main startup：在持久化消费者线程就绪之前允许直连 SQLite（见 startup_sqlite_writes_bypass_queue）
_sqlite_bypass_depth: ContextVar[int] = ContextVar("sqlite_bypass_depth", default=0)
_bypass_usage: Counter[str] = Counter()
_bypass_usage_lock = threading.Lock()
T = TypeVar("T")


def register_sqlite_writer_thread() -> None:
    global _sqlite_writer_thread_ident
    _sqlite_writer_thread_ident = threading.get_ident()


def clear_sqlite_writer_thread() -> None:
    global _sqlite_writer_thread_ident
    _sqlite_writer_thread_ident = None


def is_sqlite_writer_thread() -> bool:
    return (
        _sqlite_writer_thread_ident is not None
        and threading.get_ident() == _sqlite_writer_thread_ident
    )


@contextlib.contextmanager
def sqlite_writes_bypass_queue(*, reason: str):
    """Allow direct writes in this context, recording the explicit exception.

    asyncio tasks and to_thread inherit a copied context; bare threads and
    run_in_executor do not. Prefer the writer queue for runtime operations.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("SQLite bypass requires a non-empty reason")
    reason = reason.strip()
    token = _sqlite_bypass_depth.set(_sqlite_bypass_depth.get() + 1)
    with _bypass_usage_lock:
        _bypass_usage[reason] += 1
    logger.info("SQLite bypass entered: reason=%s depth=%d", reason, _sqlite_bypass_depth.get())
    try:
        yield
    finally:
        _sqlite_bypass_depth.reset(token)
        logger.debug("SQLite bypass exited: reason=%s", reason)


def get_sqlite_bypass_usage() -> dict[str, int]:
    """Return observed entries by reason; counters never grant write access."""
    with _bypass_usage_lock:
        return dict(_bypass_usage)


@contextlib.contextmanager
def startup_sqlite_writes_bypass_queue():
    """在持久化消费者线程启动前直连 SQLite 的兼容入口。"""
    with sqlite_writes_bypass_queue(reason="startup.bootstrap"):
        yield


def allow_direct_sqlite_writes() -> bool:
    """脚本 / 迁移 / 启动早期 bypass / 个别单测可走直连写库；正式运行时默认走队列。"""
    if _sqlite_bypass_depth.get() > 0:
        return True
    return SQLiteWriteEnvironmentSettings.from_env().direct_writes


def run_on_sqlite_writer(operation: Callable[[], T], *, timeout: float = 30.0) -> T:
    """Wait for an API-process writer operation, including its committed reads.

    Explicit migration/test direct-write settings remain supported. Callers in
    an async endpoint must use the async wrapper to avoid blocking its loop.
    """
    if is_sqlite_writer_thread() or allow_direct_sqlite_writes():
        return operation()
    from application.engine.services.persistence_queue import get_persistence_queue

    return get_persistence_queue().call_on_writer(operation, timeout=timeout)


async def run_on_sqlite_writer_async(operation: Callable[[], T]) -> T:
    return await asyncio.to_thread(run_on_sqlite_writer, operation)


def strip_sql_comments(sql: str) -> str:
    text = re.sub(r"--[^\n]*", "", sql)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def sql_is_mutating(sql: str) -> bool:
    s = strip_sql_comments(sql).strip().upper()
    if not s:
        return False
    if s.startswith("WITH"):
        return bool(re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", s))
    tok = s.split()[0]
    non_mutating = frozenset(
        {
            "SELECT",
            "EXPLAIN",
            "PRAGMA",
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
            "SAVEPOINT",
            "RELEASE",
            "END",
        }
    )
    if tok in non_mutating:
        return False
    return True


Params = Union[tuple, list]


class _EnqueuedStmtCursor:
    """非 writer 线程上 execute(INSERT/UPDATE…) 入队后对调用方的最小兼容。"""

    rowcount = 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class TxnCollectingConnection:
    """在 API 线程上收集 SQL，退出 `transaction()` 时一次性 EXECUTE_SQL_TXN_BATCH。"""

    def __init__(self) -> None:
        self.operations: List[Tuple[str, tuple]] = []

    def execute(self, sql: str, params: Any = ()) -> _EnqueuedStmtCursor:
        if isinstance(params, list):
            p: tuple = tuple(params)
        elif isinstance(params, tuple):
            p = params
        else:
            p = (params,)
        self.operations.append((sql, p))
        return _EnqueuedStmtCursor()


def enqueue_execute_sql(sql: str, params: Optional[Params] = None) -> bool:
    from application.engine.services.persistence_queue import (
        get_persistence_queue,
        PersistenceCommandType,
    )

    pq = get_persistence_queue()
    if pq is None or pq.get_queue() is None:
        logger.error("持久化队列未就绪，丢弃写 SQL")
        return False
    plist = list(params) if params is not None else []
    return pq.push(
        PersistenceCommandType.EXECUTE_SQL.value,
        {"sql": sql, "params": plist},
    )


def enqueue_txn_batch(
    operations: List[Tuple[str, Params]],
) -> bool:
    """多语句同一 BEGIN IMMEDIATE 事务提交（高性能、原子）。"""
    from application.engine.services.persistence_queue import (
        get_persistence_queue,
        PersistenceCommandType,
    )

    if not operations:
        return True
    pq = get_persistence_queue()
    if pq is None or pq.get_queue() is None:
        logger.error("持久化队列未就绪，丢弃事务批量写")
        return False
    serializable = [
        {"sql": op[0], "params": list(op[1]) if op[1] is not None else []}
        for op in operations
    ]
    return pq.push(
        PersistenceCommandType.EXECUTE_SQL_TXN_BATCH.value,
        {"operations": serializable},
    )


def enqueue_delete_chapter(chapter_db_id: str) -> bool:
    from application.engine.services.persistence_queue import (
        get_persistence_queue,
        PersistenceCommandType,
    )

    pq = get_persistence_queue()
    if pq is None or pq.get_queue() is None:
        return False
    return pq.push(
        PersistenceCommandType.DELETE_CHAPTER.value,
        {"chapter_db_id": chapter_db_id},
    )
