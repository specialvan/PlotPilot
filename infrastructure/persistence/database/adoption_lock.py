"""Local SQLite commit execution locks, released by the OS after a crash.

All processes using a database must share its local lock directory. Lock files
are deliberately retained: deleting a locked inode can admit a second owner.
"""
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import threading
import time

from application.ai_invocation.continuation import CommitInProgress

_memory_guard = threading.Lock()
_memory_locks: dict[tuple[int, str], threading.Lock] = {}


@contextmanager
def adoption_execution_lock(db, commit_id: str, *, timeout: float = 30.0):
    path = getattr(db, "db_path", None)
    if not path or str(path) == ":memory:":
        with _memory_guard:
            lock = _memory_locks.setdefault((id(db), commit_id), threading.Lock())
        if not lock.acquire(timeout=timeout):
            raise CommitInProgress(commit_id)
        try:
            yield
        finally:
            lock.release()
        return

    database = Path(path).resolve()
    directory = database.parent / ".adoption-locks"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{database.name}:{commit_id}".encode()).hexdigest()
    with (directory / f"{digest}.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise CommitInProgress(commit_id) from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
