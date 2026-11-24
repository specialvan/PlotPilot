"""启动自动驾驶守护进程（v2，全依赖注入 + 护城河）

日志：默认与 API 共用 ``logs/plotpilot.log``（环境变量 LOG_FILE），便于在「主日志」里查看
规划/写作/节拍；另可设 LOG_FILE 仅写文件。
"""
import os
import sys
import logging
import time
import argparse
import json
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _isolated_smoke_test() -> int:
    """Set data paths before any application imports, inside a fresh process."""
    with tempfile.TemporaryDirectory(prefix="plotpilot-daemon-smoke-") as directory:
        environment = os.environ.copy()
        environment.update({
            "PLOTPILOT_PROD_DATA_DIR": directory,
            "PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES": "0",
            "AITEXT_ALLOW_DIRECT_SQLITE_WRITES": "0",
            "VECTOR_STORE_ENABLED": "false",
            "LOG_FILE": str(Path(directory) / "daemon.log"),
            "PYTHONIOENCODING": "utf-8",
        })
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--smoke-worker"],
                                env=environment, timeout=90, capture_output=True, encoding="utf-8", errors="replace")
        if result.returncode:
            sys.stderr.write(result.stdout + result.stderr)
        else:
            print(result.stdout.strip())
        return result.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-test", action="store_true", help="在临时数据目录验证完整启动、队列写入和停止")
    parser.add_argument("--smoke-worker", action="store_true", help=argparse.SUPPRESS)
    _arguments = parser.parse_args()
    if _arguments.smoke_test:
        raise SystemExit(_isolated_smoke_test())

from infrastructure.ai.process_environment import configure_huggingface_process_environment

configure_huggingface_process_environment()

from dotenv import load_dotenv

load_dotenv()

from application.paths import PLOTPILOT_ROOT, get_db_path, DATA_DIR
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from infrastructure.persistence.database.chapter_element_repository import ChapterElementRepository
from infrastructure.persistence.database.sqlite_foreshadowing_repository import SqliteForeshadowingRepository
from infrastructure.persistence.database.sqlite_storyline_repository import SqliteStorylineRepository
from infrastructure.persistence.database.sqlite_plot_arc_repository import SqlitePlotArcRepository
from infrastructure.persistence.database.sqlite_narrative_event_repository import SqliteNarrativeEventRepository

from application.engine.services.background_task_service import BackgroundTaskService
from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.engine.services.circuit_breaker import CircuitBreaker
from application.blueprint.services.continuous_planning_service import ContinuousPlanningService

# 复用 API 层的工厂函数，保证与 FastAPI 层使用同一套配置
from interfaces.api.dependencies import (
    get_llm_service,
    build_auto_workflow,
    get_context_builder,
    get_bible_service,
    get_foreshadowing_repository,
    get_novel_repository,
    get_chapter_repository,
    get_voice_drift_service,
    get_knowledge_service,
    get_chapter_indexing_service,
)
from interfaces.api.middleware.logging_config import parse_log_level, setup_logging
from interfaces.api.settings import get_backend_settings

(DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
_settings = get_backend_settings()
_log_level = parse_log_level(_settings.log_level)
_default_log = str(PLOTPILOT_ROOT / "logs" / "plotpilot.log")
_log_file = _settings.log_file
if _log_file == "logs/plotpilot.log":
    _log_file = _default_log
setup_logging(level=_log_level, log_file=_log_file)

logger = logging.getLogger(__name__)


def build_daemon():
    db_path = get_db_path()
    db = get_database(db_path)

    novel_repo = SqliteNovelRepository(db)
    chapter_repo = SqliteChapterRepository(db)
    story_node_repo = StoryNodeRepository(db_path)
    chapter_element_repo = ChapterElementRepository(db_path)
    foreshadow_repo = SqliteForeshadowingRepository(db)

    llm_service = get_llm_service()
    chapter_workflow = build_auto_workflow(llm_service)
    context_builder = get_context_builder()

    planning_service = ContinuousPlanningService(
        story_node_repo=story_node_repo,
        chapter_element_repo=chapter_element_repo,
        llm_service=llm_service,
        bible_service=get_bible_service(),
        chapter_repository=chapter_repo,
        novel_repository=novel_repo,
    )

    # VoiceDriftService：与 FastAPI get_voice_drift_service() 同源（chapter_style_scores.upsert，勿用 VoiceVault）
    voice_drift_service = None
    try:
        voice_drift_service = get_voice_drift_service()
        logger.info("VoiceDriftService 已启用（与 API 同源注入）")
    except Exception as e:
        logger.warning(f"VoiceDriftService 初始化失败，文风检测已禁用：{e}")

    # TripleRepository（可选）
    triple_repo = None
    try:
        from infrastructure.persistence.database.triple_repository import TripleRepository
        triple_repo = TripleRepository(db)
        logger.info("TripleRepository 已启用")
    except Exception as e:
        logger.warning(f"TripleRepository 不可用，图谱提取已禁用：{e}")

    bg_service = BackgroundTaskService(
        voice_drift_service=voice_drift_service,
        llm_service=llm_service,
        foreshadowing_repo=foreshadow_repo,
        triple_repository=triple_repo,
        knowledge_service=get_knowledge_service(),
        chapter_indexing_service=get_chapter_indexing_service(),
        storyline_repository=SqliteStorylineRepository(get_database()),
        chapter_repository=get_chapter_repository(),
        plot_arc_repository=SqlitePlotArcRepository(get_database()),
        narrative_event_repository=SqliteNarrativeEventRepository(get_database()),
    )

    aftermath_pipeline = None
    try:
        # ★ V8 Feed-forward: 因果边 / 人物状态 / 叙事债务 仓储
        causal_edge_repo = None
        character_state_repo = None
        debt_repo = None
        bible_repo = None

        try:
            from infrastructure.persistence.database.sqlite_causal_edge_repository import SqliteCausalEdgeRepository
            causal_edge_repo = SqliteCausalEdgeRepository(get_database())
        except Exception as e:
            logger.warning(f"CausalEdgeRepository 初始化失败: {e}")

        try:
            from infrastructure.persistence.database.sqlite_character_state_repository import SqliteCharacterStateRepository
            character_state_repo = SqliteCharacterStateRepository(get_database())
        except Exception as e:
            logger.warning(f"CharacterStateRepository 初始化失败: {e}")

        try:
            from infrastructure.persistence.database.sqlite_narrative_debt_repository import SqliteNarrativeDebtRepository
            debt_repo = SqliteNarrativeDebtRepository(get_database())
        except Exception as e:
            logger.warning(f"NarrativeDebtRepository 初始化失败: {e}")

        try:
            from interfaces.api.dependencies import get_bible_repository
            bible_repo = get_bible_repository()
        except Exception as e:
            logger.warning(f"BibleRepository 初始化失败: {e}")

        unified_checkpoint_svc = None
        try:
            from interfaces.api.dependencies import get_unified_checkpoint_service
            unified_checkpoint_svc = get_unified_checkpoint_service()
        except Exception as e:
            logger.warning(f"UnifiedCheckpointService 初始化失败: {e}")

        aftermath_pipeline = ChapterAftermathPipeline(
            knowledge_service=get_knowledge_service(),
            chapter_indexing_service=get_chapter_indexing_service(),
            llm_service=llm_service,
            voice_drift_service=voice_drift_service,
            triple_repository=triple_repo,
            foreshadowing_repository=foreshadow_repo,
            storyline_repository=SqliteStorylineRepository(get_database()),
            chapter_repository=get_chapter_repository(),
            plot_arc_repository=SqlitePlotArcRepository(get_database()),
            narrative_event_repository=SqliteNarrativeEventRepository(get_database()),
            causal_edge_repository=causal_edge_repo,
            character_state_repository=character_state_repo,
            debt_repository=debt_repo,
            bible_repository=bible_repo,
            unified_checkpoint_service=unified_checkpoint_svc,
        )
        logger.info("ChapterAftermathPipeline 已注入（叙事/向量/文风/KG；三元组/伏笔/故事线/张力/对话/因果边/人物状态/债务 单次 LLM）")
    except Exception as e:
        logger.warning("ChapterAftermathPipeline 初始化失败，审计将降级：%s", e)

    # 熔断器配置：适应 API 限流
    # - failure_threshold: 允许连续失败的次数（增大以容忍临时限流）
    # - reset_timeout: 熔断后等待恢复的时间（秒）
    circuit_breaker = CircuitBreaker(
        failure_threshold=10,  # 从 5 增加到 10，更宽容临时限流
        reset_timeout=180,     # 从 120 增加到 180 秒，给 API 更多恢复时间
    )

    from engine.runtime.engine_daemon import EngineDaemon
    from engine.runtime.writing_delegate import (
        get_story_pipeline_mode,
        story_pipeline_mode_was_unset,
    )

    pipeline_mode = get_story_pipeline_mode()
    daemon_kwargs = dict(
        novel_repository=novel_repo,
        llm_service=llm_service,
        context_builder=context_builder,
        background_task_service=bg_service,
        planning_service=planning_service,
        story_node_repo=story_node_repo,
        chapter_repository=chapter_repo,
        poll_interval=10,
        voice_drift_service=voice_drift_service,
        circuit_breaker=circuit_breaker,
        chapter_workflow=chapter_workflow,
        aftermath_pipeline=aftermath_pipeline,
        knowledge_service=get_knowledge_service(),
    )

    use_pipeline_writing = pipeline_mode in ("writing", "full")
    if pipeline_mode == "full":
        logger.info(
            "PLOTPILOT_USE_STORY_PIPELINE=full — EngineDaemon（StoryPipeline 写作）"
        )
    elif pipeline_mode == "writing":
        if story_pipeline_mode_was_unset():
            logger.info(
                "PLOTPILOT_USE_STORY_PIPELINE 未设置 — 默认 StoryPipeline 写作（Phase 4）"
            )
        else:
            logger.info(
                "PLOTPILOT_USE_STORY_PIPELINE=writing — EngineDaemon（StoryPipeline 写作）"
            )
    else:
        logger.info(
            "PLOTPILOT_USE_STORY_PIPELINE=off — EngineDaemon（legacy 节拍写作，紧急回退）"
        )

    return EngineDaemon(
        **daemon_kwargs,
        use_story_pipeline_for_writing=use_pipeline_writing,
    )


def _smoke_lifecycle(daemon, queue, db):
    started = threading.Event()
    stop_event = threading.Event()
    failures = []

    class StartedHandler(logging.Handler):
        def emit(self, record):
            if record.name == "engine.runtime.daemon_loop" and record.getMessage().startswith("Loop #1:"):
                started.set()

    handler = StartedHandler()
    logging.getLogger("engine.runtime.daemon_loop").addHandler(handler)
    daemon.runner.poll_interval = 0.05

    def run():
        try:
            daemon.run_forever(stop_event=stop_event)
        except BaseException as exc:
            failures.append(exc)
            started.set()

    thread = threading.Thread(target=run, name="DaemonSmoke", daemon=True)
    thread.start()
    try:
        if not started.wait(20) or failures:
            raise RuntimeError(f"Daemon failed to start: {failures}")
        # Use the normal serializable SQL command, then an acknowledged writer
        # barrier and a separate reader. The smoke never enables runtime bypass.
        db.execute("INSERT INTO novels (id, title, slug, autopilot_status) VALUES (?, ?, ?, ?)",
                   ("daemon-smoke", "Daemon smoke", "daemon-smoke", "paused"))
        queue.call_on_writer(lambda: None)
        row = db.fetch_one("SELECT title FROM novels WHERE id = ?", ("daemon-smoke",))
        if row is None or row["title"] != "Daemon smoke":
            raise RuntimeError("Daemon smoke write was not committed")
    finally:
        stop_event.set()
        thread.join(timeout=10)
        logging.getLogger("engine.runtime.daemon_loop").removeHandler(handler)
    if thread.is_alive() or failures:
        raise RuntimeError(f"Daemon did not stop cleanly: {failures}")
    return {"started": True, "write_committed": True, "stopped": True, "database": str(db.db_path)}


def main(*, smoke_test=False):
    from application.engine.services.persistence_queue import get_persistence_queue, register_persistence_handlers
    from infrastructure.persistence.database.adoption_dispatch import register_adoption_commit_handler
    from infrastructure.persistence.database.write_dispatch import startup_sqlite_writes_bypass_queue, run_on_sqlite_writer

    queue = get_persistence_queue()
    queue.initialize()
    register_persistence_handlers()
    register_adoption_commit_handler(queue)
    db = None
    try:
        with startup_sqlite_writes_bypass_queue():
            db = get_database(get_db_path())
        queue.start_consumer()
        daemon = run_on_sqlite_writer(build_daemon)
        if smoke_test:
            result = _smoke_lifecycle(daemon, queue, db)
        else:
            stop_event = threading.Event()
            previous = {}
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda *_: stop_event.set())
            try:
                daemon.run_forever(stop_event=stop_event)
            finally:
                for signum, old_handler in previous.items():
                    signal.signal(signum, old_handler)
            result = None
    finally:
        queue.stop_consumer()
        if queue.is_consumer_running():
            raise RuntimeError("Daemon SQLite consumer did not stop")
        queue.get_queue().close()
        queue.get_queue().join_thread()
        if db is not None:
            db.close_all()
    if smoke_test:
        print(json.dumps({**result, "writer_stopped": True}, ensure_ascii=False))


if __name__ == "__main__":
    main(smoke_test=_arguments.smoke_worker)
