"""启动早期直连 SQLite bypass（与 persistence consumer 次序解耦）。"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from infrastructure.persistence.database import write_dispatch as wd


@pytest.fixture
def clear_direct_write_env(monkeypatch):
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)


def test_startup_bypass_enables_allow_direct_writes(clear_direct_write_env):
    assert wd.allow_direct_sqlite_writes() is False

    with wd.startup_sqlite_writes_bypass_queue():
        assert wd.allow_direct_sqlite_writes() is True

    assert wd.allow_direct_sqlite_writes() is False


def test_nested_startup_bypass_restores_depth(clear_direct_write_env):
    with wd.startup_sqlite_writes_bypass_queue():
        with wd.startup_sqlite_writes_bypass_queue():
            assert wd.allow_direct_sqlite_writes() is True
        assert wd.allow_direct_sqlite_writes() is True
    assert wd.allow_direct_sqlite_writes() is False


def test_allow_direct_sqlite_writes_uses_environment_settings(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "yes")
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)

    assert wd.allow_direct_sqlite_writes() is True


def test_bypass_reason_is_required_and_audited(clear_direct_write_env, caplog):
    before = wd.get_sqlite_bypass_usage().get("test.audit", 0)
    with pytest.raises(ValueError):
        with wd.sqlite_writes_bypass_queue(reason=" "):
            pass
    with caplog.at_level(logging.INFO, logger=wd.__name__):
        with pytest.raises(RuntimeError, match="failure"):
            with wd.sqlite_writes_bypass_queue(reason="test.audit"):
                assert wd.allow_direct_sqlite_writes()
                raise RuntimeError("failure")
    assert not wd.allow_direct_sqlite_writes()
    assert wd.get_sqlite_bypass_usage()["test.audit"] == before + 1
    assert "reason=test.audit" in caplog.text


def test_thread_a_bypass_does_not_authorize_thread_b(clear_direct_write_env):
    entered = threading.Event()
    checked = threading.Event()

    def thread_a():
        with wd.sqlite_writes_bypass_queue(reason="test.thread_a"):
            entered.set()
            assert checked.wait(3)
            return wd.allow_direct_sqlite_writes()

    def thread_b():
        assert entered.wait(3)
        try:
            return wd.allow_direct_sqlite_writes()
        finally:
            checked.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(thread_a)
        b = pool.submit(thread_b)
        assert a.result(timeout=5) is True
        assert b.result(timeout=5) is False
    assert not wd.allow_direct_sqlite_writes()


@pytest.mark.asyncio
async def test_bypass_context_propagation(clear_direct_write_env):
    bare_thread_access = []
    with wd.sqlite_writes_bypass_queue(reason="test.propagation"):
        assert await asyncio.to_thread(wd.allow_direct_sqlite_writes) is True
        assert await asyncio.get_running_loop().run_in_executor(None, wd.allow_direct_sqlite_writes) is False
        thread = threading.Thread(target=lambda: bare_thread_access.append(wd.allow_direct_sqlite_writes()))
        thread.start()
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert bare_thread_access == [False]
    assert not await asyncio.to_thread(wd.allow_direct_sqlite_writes)


@pytest.mark.asyncio
async def test_bypass_isolated_between_async_tasks(clear_direct_write_env):
    entered = asyncio.Event()
    checked = asyncio.Event()

    async def permitted():
        with wd.sqlite_writes_bypass_queue(reason="test.task_a"):
            entered.set()
            await checked.wait()
            return wd.allow_direct_sqlite_writes()

    async def denied():
        await entered.wait()
        try:
            return wd.allow_direct_sqlite_writes()
        finally:
            checked.set()

    assert await asyncio.wait_for(asyncio.gather(permitted(), denied()), timeout=3) == [True, False]
