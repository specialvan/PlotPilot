import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import threading

import pytest

from application.engine.services.persistence_queue import PersistenceQueue
from infrastructure.persistence.database import write_dispatch as wd


@pytest.fixture
def writer(monkeypatch):
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    queue = PersistenceQueue()
    queue.initialize()
    monkeypatch.setattr("application.engine.services.persistence_queue._persistence_queue", queue)
    queue.start_consumer()
    try:
        yield queue
    finally:
        queue.stop_consumer()
        queue.get_queue().close()
        queue.get_queue().join_thread()


def test_writer_acknowledges_result_and_propagates_error_once(writer):
    caller = threading.get_ident()
    assert wd.run_on_sqlite_writer(lambda: (wd.is_sqlite_writer_thread(), threading.get_ident())) == (
        True, writer._consumer_thread.ident,
    )
    assert caller != writer._consumer_thread.ident
    calls = []

    def fail():
        calls.append(1)
        raise ValueError("effect failed")

    with pytest.raises(ValueError, match="effect failed"):
        wd.run_on_sqlite_writer(fail)
    assert calls == [1]
    assert wd.run_on_sqlite_writer(lambda: wd.run_on_sqlite_writer(lambda: 42)) == 42


def test_pending_timed_out_operation_does_not_execute(writer):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking():
        entered.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(writer.call_on_writer, blocking)
        assert entered.wait(3)
        try:
            with pytest.raises(TimeoutError):
                writer.call_on_writer(lambda: calls.append(1), timeout=0.01)
        finally:
            release.set()
        running.result(timeout=5)
    assert writer.call_on_writer(lambda: calls) == []
    assert not writer._local_calls


@pytest.mark.asyncio
async def test_async_writer_wait_keeps_event_loop_responsive(writer):
    entered = threading.Event()
    release = threading.Event()

    def blocking():
        entered.set()
        assert release.wait(3)
        return "saved"

    task = asyncio.create_task(wd.run_on_sqlite_writer_async(blocking))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done()
        await asyncio.sleep(0)
    finally:
        release.set()
    assert await task == "saved"


def test_stopped_consumer_rejects_calls_and_revokes_writer(writer):
    writer.stop_consumer()
    assert wd._sqlite_writer_thread_ident is None
    with pytest.raises(RuntimeError, match="not running"):
        writer.call_on_writer(lambda: 1)
