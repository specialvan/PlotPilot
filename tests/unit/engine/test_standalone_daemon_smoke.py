import json
from pathlib import Path
import subprocess
import sys


def test_standalone_daemon_starts_writes_and_stops_in_isolated_directory():
    root = Path(__file__).resolve().parents[3]
    process = subprocess.run([sys.executable, "scripts/start_daemon.py", "--smoke-test"],
                             cwd=root, capture_output=True, text=True, timeout=100)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.strip())
    assert all(result[key] for key in ("started", "write_committed", "stopped", "writer_stopped"))
    assert Path(result["database"]).parent != root / "data"
    assert not Path(result["database"]).exists()
