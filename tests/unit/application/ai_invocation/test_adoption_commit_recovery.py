import asyncio
from dataclasses import replace
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import threading

from fastapi import FastAPI
import httpx
import pytest

from application.ai_invocation.continuation import register_continuation_handler
from application.ai_invocation.dtos import (
    AdoptionCommit, AdoptionCommitStatus, AdoptionCommitStep, AdoptionDecision,
    ContinuationRef, InvocationAttempt, InvocationAttemptStatus, InvocationPolicy,
    InvocationSession, InvocationSessionStatus, PromptSnapshot,
)
from application.ai_invocation.services import AdoptionCommitService
from application.ai_invocation.variable_hub import VariableWrite
from application.engine.services.persistence_queue import PersistenceQueue
from domain.ai.value_objects.prompt import Prompt
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
    SqliteAdoptionRepository, SqliteInvocationAttemptRepository, SqliteInvocationSessionRepository,
    SqliteInvocationSpecRepository, SqliteVariableHubRepository,
)
from infrastructure.persistence.database import write_dispatch as wd
from interfaces.api.v1.engine import ai_invocation_routes as routes


def _remote_commit(queue, session, decision, reply):
    from application.engine.services.persistence_queue import inject_persistence_queue
    from infrastructure.persistence.database.adoption_dispatch import DispatchedAdoptionCommitService

    inject_persistence_queue(queue)
    try:
        commit = DispatchedAdoptionCommitService(None, None).commit(session=session, decision=decision)
        reply.send((commit.id, commit.status.value, session.status.value))
    except BaseException as exc:
        reply.send(("error", str(exc)))
    finally:
        reply.close()


@pytest.fixture
def commit_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    with wd.startup_sqlite_writes_bypass_queue():
        db = DatabaseConnection(str(tmp_path / "commits.db"))
        with db.transaction() as conn:
            conn.execute("CREATE TABLE test_effects (effect_key TEXT PRIMARY KEY, content TEXT)")
    queue = PersistenceQueue()
    # The same queue must be transferable to spawned workers on Unix too.
    queue._queue = mp.get_context("spawn").Queue()
    queue.initialize()
    monkeypatch.setattr("application.engine.services.persistence_queue._persistence_queue", queue)
    queue.start_consumer()
    repos = {
        "adoption": SqliteAdoptionRepository(db), "session": SqliteInvocationSessionRepository(db),
        "attempt": SqliteInvocationAttemptRepository(db), "spec": SqliteInvocationSpecRepository(db),
        "variable_hub": SqliteVariableHubRepository(db),
    }
    session = InvocationSession(
        id="s1", operation="test.commit", node_key="test", policy=InvocationPolicy.FULL_INTERACTIVE,
        continuation=ContinuationRef(handler_key="test_recoverable"),
    )
    snapshot = PromptSnapshot(prompt=Prompt(system="system", user="user"), node_key="test",
        node_version_id="", asset_link_set_id="", input_binding_set_id="", output_binding_set_id="",
        variable_snapshot_hash="", template_hash="", composition_hash="", rendered_prompt_hash="")
    attempt = InvocationAttempt(id="a1", session_id="s1", status=InvocationAttemptStatus.SUCCEEDED,
                                prompt_snapshot=snapshot, content="accepted")
    decision = AdoptionDecision(id="d1", session_id="s1", attempt_id="a1", accepted_content="accepted")
    repos["adoption"].prepare_commit(session=session, decision=decision, attempt=attempt)
    monkeypatch.setattr(routes, "_repositories", lambda: repos)
    app = FastAPI()
    app.include_router(routes.router)
    try:
        yield db, repos, session, decision, app
    finally:
        queue.stop_consumer()
        queue.get_queue().close()
        queue.get_queue().join_thread()
        db.close_all()


def install_effect(db, *, entered=None, release=None, retry_safe=True):
    calls = []

    def handler(ctx):
        calls.append(ctx.idempotency_key)
        with db.transaction() as conn:
            conn.execute("INSERT INTO test_effects VALUES (?, ?) ON CONFLICT(effect_key) DO NOTHING",
                         (ctx.idempotency_key, ctx.decision.accepted_content))
        if entered:
            entered.set()
            assert release.wait(5)
        return {"effect_key": ctx.idempotency_key}

    register_continuation_handler("test_recoverable", handler, retry_safe=retry_safe)
    return calls


def execute_write(db, sql, params=()):
    def write():
        with db.transaction() as conn:
            conn.execute(sql, params)
    return wd.run_on_sqlite_writer(write)


@pytest.mark.asyncio
async def test_same_decision_http_retry_returns_original_result(commit_env):
    db, repos, session, decision, app = commit_env
    calls = install_effect(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/ai-invocations/s1/commits", json={"decision_id": "d1"})
        second = await client.post("/ai-invocations/s1/commits", json={"decision_id": "d1"})
    assert first.status_code == second.status_code == 200
    assert first.json()["commit"] == second.json()["commit"]
    assert first.json()["commit"]["status"] == "succeeded"
    assert len(calls) == 1
    assert len(db.fetch_all("SELECT * FROM ai_adoption_commits")) == 1
    assert len(db.fetch_all("SELECT * FROM ai_adoption_commit_steps")) == 5


@pytest.mark.asyncio
async def test_concurrent_http_commit_and_event_loop_responsiveness(commit_env):
    db, repos, session, decision, app = commit_env
    entered, release = threading.Event(), threading.Event()
    calls = install_effect(db, entered=entered, release=release)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/ai-invocations/s1/commits", json={"decision_id": "d1"}))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            second = asyncio.create_task(client.post("/ai-invocations/s1/commits", json={"decision_id": "d1"}))
            # This coroutine must run while the synchronous commit is blocked.
            await asyncio.sleep(0.02)
            assert not first.done()
        finally:
            release.set()
        responses = await asyncio.gather(first, second)
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json()["commit"] == responses[1].json()["commit"]
    assert len(calls) == 1
    assert len(db.fetch_all("SELECT * FROM test_effects")) == 1


def test_save_commit_conflicts_on_key_and_keeps_canonical_id(commit_env):
    db, repos, session, decision, app = commit_env
    original = repos["adoption"].create_commit(session_id=session.id, decision_id=decision.id)
    retry = AdoptionCommit(id="different-id", session_id=session.id, decision_id=decision.id,
        status=AdoptionCommitStatus.SUCCEEDED,
        steps=[AdoptionCommitStep(name="done", status=AdoptionCommitStatus.SUCCEEDED)])
    repos["adoption"].save_commit(retry)
    assert retry.id == original.id
    assert repos["adoption"].get_commit_for_decision(decision.id).id == original.id
    assert db.fetch_one("SELECT commit_id FROM ai_adoption_commit_steps")["commit_id"] == original.id


def test_auto_commit_retains_attempt_created_after_session_was_saved(commit_env):
    db, repos, session, decision, app = commit_env
    install_effect(db)
    session.attempts.append(decision.attempt_id)
    attempt = repos["attempt"].get(decision.attempt_id)
    result = AdoptionCommitService(adoption_repository=repos["adoption"]).commit(
        session=session, decision=decision, attempt=attempt)
    assert result.status == AdoptionCommitStatus.SUCCEEDED
    assert session.attempts == [decision.attempt_id]
    assert repos["session"].get(session.id).attempts == [decision.attempt_id]


def test_replaying_successful_decision_preserves_newer_session_state(commit_env):
    db, repos, session, decision, app = commit_env
    calls = install_effect(db)
    service = AdoptionCommitService(adoption_repository=repos["adoption"])
    first = service.commit(session=session, decision=decision)
    stale = replace(session)
    session.status = InvocationSessionStatus.AWAITING_ACCEPTANCE
    session.attempts = ["a1", "later-attempt"]
    repos["adoption"].save_session(session)
    replayed = service.commit(session=stale, decision=decision)
    assert replayed == first
    assert stale.status == InvocationSessionStatus.AWAITING_ACCEPTANCE
    assert repos["session"].get(session.id).status == InvocationSessionStatus.AWAITING_ACCEPTANCE
    assert repos["session"].get(session.id).attempts == ["a1", "later-attempt"]
    assert len(calls) == 1


def test_final_session_write_failure_leaves_commit_recoverable(commit_env, monkeypatch):
    db, repos, session, decision, app = commit_env
    calls = install_effect(db)
    repo = repos["adoption"]
    save_session = repo.save_session

    def fail_final_session(value):
        if value.status == InvocationSessionStatus.COMPLETED:
            raise OSError("session write failed")
        save_session(value)

    with monkeypatch.context() as patch:
        patch.setattr(repo, "save_session", fail_final_session)
        with pytest.raises(OSError, match="session write failed"):
            AdoptionCommitService(adoption_repository=repo).commit(session=session, decision=decision)
    assert repo.get_commit_for_decision(decision.id).status == AdoptionCommitStatus.RUNNING
    recovered = AdoptionCommitService(adoption_repository=repo).commit(session=session, decision=decision)
    assert recovered.status == AdoptionCommitStatus.SUCCEEDED
    assert repos["session"].get(session.id).status == InvocationSessionStatus.COMPLETED
    assert len(calls) == 1


def test_variable_output_retry_preserves_version_and_newer_value(commit_env):
    db, repos, session, decision, app = commit_env
    repo = repos["variable_hub"]
    write = VariableWrite(key="chapter.prose.accepted", value="accepted",
                          context_key="novel_id:n1|chapter_number:1", source_commit_id="commit-1")
    first = repo.set_value(write)
    newer = repo.set_value(replace(write, value="later", source_commit_id="commit-2"))
    replayed = repo.set_value(write)
    assert replayed.version_number == first.version_number == 1
    assert replayed.value == "accepted"
    assert newer.version_number == 2
    assert repo.get_value(write.key, write.context_key).value == "later"
    assert len(db.fetch_all("SELECT * FROM variable_values")) == 2


def test_projection_receipt_preserves_later_chapter_edits(commit_env):
    from application.ai_invocation.contracts.chapter_prose_generation import project_chapter_prose_to_chapters

    db, repos, session, decision, app = commit_env
    record = repos["adoption"].create_commit(session_id=session.id, decision_id=decision.id)
    execute_write(db, "INSERT INTO novels (id, title, slug) VALUES ('n1', 'novel', 'novel')")
    projection = {"novel_id": "n1", "chapter_number": 1, "content": "accepted"}
    first = wd.run_on_sqlite_writer(lambda: project_chapter_prose_to_chapters(
        db, projection, commit_id=record.id))
    execute_write(db, "UPDATE chapters SET content = 'later edit'")
    replayed = wd.run_on_sqlite_writer(lambda: project_chapter_prose_to_chapters(
        db, projection, commit_id=record.id))
    assert replayed == first
    assert db.fetch_one("SELECT content FROM chapters")["content"] == "later edit"
    assert len(db.fetch_all("SELECT * FROM chapters")) == 1


def test_daemon_process_commits_are_acknowledged_by_writer_owner(commit_env):
    db, repos, session, decision, app = commit_env
    from application.engine.services.persistence_queue import get_persistence_queue
    from infrastructure.persistence.database.adoption_dispatch import register_adoption_commit_handler

    queue = get_persistence_queue()
    register_adoption_commit_handler(queue, db=db)
    calls = install_effect(db)
    processes = []
    readers = []
    try:
        for _ in range(2):
            reader, sender = mp.Pipe(duplex=False)
            process = mp.get_context("spawn").Process(
                target=_remote_commit, args=(queue.get_queue(), session, decision, sender))
            process.start()
            sender.close()
            readers.append(reader)
            processes.append(process)
        responses = []
        for reader in readers:
            assert reader.poll(25), "daemon did not receive writer acknowledgement"
            responses.append(reader.recv())
        assert responses[0] == responses[1]
        assert responses[0][1:] == ("succeeded", "completed")
        assert len(calls) == 1
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            assert process.exitcode == 0
        for reader in readers:
            reader.close()


@pytest.mark.parametrize("retry_safe", [True, False])
def test_process_crash_after_effect_before_checkpoint(commit_env, retry_safe):
    db, repos, session, decision, app = commit_env
    script = r'''
import os, sys
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteAdoptionRepository, SqliteInvocationSessionRepository, SqliteVariableHubRepository
from infrastructure.persistence.database.write_dispatch import startup_sqlite_writes_bypass_queue
from application.engine.services.persistence_queue import get_persistence_queue
from application.ai_invocation.continuation import register_continuation_handler
from application.ai_invocation.services import AdoptionCommitService
with startup_sqlite_writes_bypass_queue():
    db = DatabaseConnection(sys.argv[1])
queue = get_persistence_queue()
queue.initialize()
queue.start_consumer()
repo = SqliteAdoptionRepository(db)
def crash(ctx):
    with db.transaction() as conn:
        conn.execute("INSERT INTO test_effects VALUES (?, ?)", (ctx.idempotency_key, ctx.decision.accepted_content))
    os._exit(29)
register_continuation_handler("test_recoverable", crash, retry_safe=True)
AdoptionCommitService(adoption_repository=repo, variable_hub_repository=SqliteVariableHubRepository(db)).commit(
    session=SqliteInvocationSessionRepository(db).get("s1"), decision=repo.get_decision("d1"))
'''
    process = subprocess.run([sys.executable, "-c", script, str(db.db_path)],
        cwd=Path(__file__).resolve().parents[4], capture_output=True, timeout=30)
    assert process.returncode == 29, process.stderr.decode(errors="replace")
    interrupted = repos["adoption"].get_commit_for_decision("d1")
    assert interrupted.status == AdoptionCommitStatus.RUNNING
    assert interrupted.steps[-1].status == AdoptionCommitStatus.RUNNING
    assert len(db.fetch_all("SELECT * FROM test_effects")) == 1
    calls = install_effect(db, retry_safe=retry_safe)
    recovered = AdoptionCommitService(adoption_repository=repos["adoption"],
        variable_hub_repository=repos["variable_hub"]).commit(session=session, decision=decision)
    assert recovered.id == interrupted.id
    assert len(db.fetch_all("SELECT * FROM test_effects")) == 1
    if retry_safe:
        assert recovered.status == AdoptionCommitStatus.SUCCEEDED
        assert len(calls) == 1
    else:
        assert recovered.status == AdoptionCommitStatus.FAILED
        assert "compensate" in recovered.error
        assert not calls
    assert len([step for step in recovered.steps if step.name == "commit_content_patch"]) == 1


@pytest.mark.parametrize("full_chapter", [False, True])
def test_builtin_prose_recovers_atomic_receipt_after_process_crash(commit_env, monkeypatch, full_chapter):
    from application.ai_invocation.autopilot import continuations

    db, repos, session, decision, app = commit_env
    session = replace(session, id="prose-session", policy=InvocationPolicy.AUTOPILOT_PAUSE,
        operation="autopilot.chapter.prose" if full_chapter else "autopilot.prose.from_script",
        context={"novel_id": "n1", "chapter_number": 1, "beat_index": 0},
        continuation=ContinuationRef(handler_key="autopilot_prose_generation"))
    attempt = replace(repos["attempt"].get("a1"), id="prose-attempt", session_id=session.id)
    decision = replace(decision, id="prose-decision", session_id=session.id, attempt_id=attempt.id)
    repos["adoption"].prepare_commit(session=session, decision=decision, attempt=attempt)
    execute_write(db, "INSERT INTO novels (id, title, slug) VALUES ('n1', 'novel', 'novel')")
    script = r'''
import os, sys
from infrastructure.persistence.database import connection
from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteAdoptionRepository, SqliteInvocationSessionRepository
from infrastructure.persistence.database.write_dispatch import startup_sqlite_writes_bypass_queue
from application.engine.services.persistence_queue import get_persistence_queue
from application.ai_invocation.autopilot import continuations
from application.ai_invocation.services import AdoptionCommitService
with startup_sqlite_writes_bypass_queue():
    db = connection.DatabaseConnection(sys.argv[1])
connection.get_database = lambda *_args, **_kwargs: db
queue = get_persistence_queue()
queue.initialize()
queue.start_consumer()
original = continuations._write_chapter_draft
def crash_after_write(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(29)
continuations._write_chapter_draft = crash_after_write
continuations.register_autopilot_continuations()
repo = SqliteAdoptionRepository(db)
AdoptionCommitService(adoption_repository=repo).commit(
    session=SqliteInvocationSessionRepository(db).get("prose-session"), decision=repo.get_decision("prose-decision"))
'''
    process = subprocess.run([sys.executable, "-c", script, str(db.db_path)],
        cwd=Path(__file__).resolve().parents[4], capture_output=True, timeout=30)
    assert process.returncode == 29, process.stderr.decode(errors="replace")
    chapter = db.fetch_one("SELECT content FROM chapters")
    novel = db.fetch_one("SELECT current_stage, current_beat_index, current_auto_chapters FROM novels")
    assert chapter["content"] == decision.accepted_content
    assert novel == {"current_stage": "auditing" if full_chapter else "writing",
                     "current_beat_index": 0 if full_chapter else 1,
                     "current_auto_chapters": 1 if full_chapter else 0}
    # Later work must survive recovery of the old, unfinished outer checkpoint.
    def advance():
        with db.transaction() as conn:
            conn.execute("UPDATE chapters SET content = ?", (decision.accepted_content + "\n\nlater beat",))
            conn.execute("UPDATE novels SET current_beat_index = 2, current_stage = 'writing'")
    wd.run_on_sqlite_writer(advance)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda *_args, **_kwargs: db)
    monkeypatch.setattr(continuations, "_clear_invocation_state",
                        lambda *_args, **_kwargs: pytest.fail("receipt replay must preserve later shared state"))
    continuations.register_autopilot_continuations()
    recovered = AdoptionCommitService(adoption_repository=repos["adoption"]).commit(session=session, decision=decision)
    assert recovered.status == AdoptionCommitStatus.SUCCEEDED
    assert db.fetch_one("SELECT content FROM chapters")["content"] == decision.accepted_content + "\n\nlater beat"
    novel = db.fetch_one("SELECT current_stage, current_beat_index, current_auto_chapters FROM novels")
    assert novel == {"current_stage": "writing", "current_beat_index": 2,
                     "current_auto_chapters": 1 if full_chapter else 0}
