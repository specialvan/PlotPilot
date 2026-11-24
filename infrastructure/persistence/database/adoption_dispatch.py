"""Acknowledged adoption commits from daemon processes to the SQLite owner."""
import logging

from application.ai_invocation.services import AdoptionCommitService
from application.engine.services.persistence_queue import get_persistence_queue
from infrastructure.persistence.database.connection import get_database

logger = logging.getLogger(__name__)
COMMAND = "adoption_commit"


class DispatchedAdoptionCommitService:
    def __init__(self, repository, variable_hub_repository):
        self._service = AdoptionCommitService(
            adoption_repository=repository, variable_hub_repository=variable_hub_repository,
        )

    def commit(self, *, session, decision, attempt=None):
        queue = get_persistence_queue()
        if not queue.uses_remote_writer:
            return self._service.commit(session=session, decision=decision, attempt=attempt)
        saved_session, commit = queue.request_result(COMMAND, {
            "session": session, "decision": decision, "attempt": attempt,
        })
        session.__dict__.update(saved_session.__dict__)
        return commit


def register_adoption_commit_handler(queue, *, db=None):
    def handler(payload):
        reply = payload["reply"]

        def execute():
            try:
                from application.ai_invocation.autopilot.continuations import register_autopilot_continuations
                from application.ai_invocation.contracts.chapter_prose_generation import register_chapter_prose_generation_continuation
                from application.blueprint.services.setup_main_plot_continuation import register_setup_main_plot_continuation
                from application.blueprint.services.setup_plot_outline_continuation import register_setup_plot_outline_continuation
                from application.world.services.bible_setup_continuation import register_bible_setup_continuations
                from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
                    SqliteAdoptionRepository, SqliteVariableHubRepository,
                )

                register_autopilot_continuations()
                register_chapter_prose_generation_continuation()
                register_setup_main_plot_continuation()
                register_setup_plot_outline_continuation()
                register_bible_setup_continuations()
                database = db if db is not None else get_database()
                service = AdoptionCommitService(adoption_repository=SqliteAdoptionRepository(database),
                                                variable_hub_repository=SqliteVariableHubRepository(database))
                session = payload["session"]
                result = service.commit(session=session, decision=payload["decision"], attempt=payload.get("attempt"))
                response = (True, (session, result))
            except BaseException as exc:
                logger.exception("Remote adoption commit failed")
                response = (False, str(exc))
            try:
                reply.send(response)
            except (EOFError, OSError):
                logger.info("Adoption requester disconnected; durable result retained for retry")
            finally:
                reply.close()

        try:
            queue.submit_job(execute)
        except RuntimeError as exc:
            try:
                reply.send((False, str(exc)))
            finally:
                reply.close()

    queue.register_handler(COMMAND, handler)
