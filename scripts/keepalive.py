"""Server keepalive — crash monitor that restarts the server if it dies.

Run detached:  python -m scripts.keepalive
Checks every 60s; if the server is down for 2 consecutive checks, restarts
it and logs to the DB. Exits when the stop-flag file appears.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOP_FILE = ROOT / "data" / "KEEPALIVE_STOP"
CHECK_INTERVAL_S = 60
MISSES_BEFORE_RESTART = 2
HEALTH_URL = "http://127.0.0.1:7878/api/health"


def _server_up() -> bool:
    import httpx
    try:
        r = httpx.get(HEALTH_URL, timeout=5)
        return r.status_code == 200
    except Exception:  # noqa: BLE001 — any error = down
        return False


def _restart_server() -> None:
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        venv_python = Path(sys.executable)
    log = open(ROOT / "data" / "server_keepalive.log", "ab")
    subprocess.Popen(
        [str(venv_python), "-m", "openstanley.server"],
        cwd=str(ROOT), stdout=log, stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _log(msg: str) -> None:
    try:
        from openstanley.core import db
        db.init_db()
        db.log("keepalive", msg)
    except Exception:  # noqa: BLE001 — keepalive never dies on logging
        pass


def main() -> None:
    _log("keepalive started (60s checks, restart after 2 misses)")
    misses = 0
    while not STOP_FILE.exists():
        if _server_up():
            misses = 0
        else:
            misses += 1
            if misses >= MISSES_BEFORE_RESTART:
                _log(f"server down {misses} checks — restarting")
                _restart_server()
                misses = 0
                time.sleep(45)  # boot grace period
        time.sleep(CHECK_INTERVAL_S)
    _log("keepalive stopped (stop flag)")


if __name__ == "__main__":
    main()
