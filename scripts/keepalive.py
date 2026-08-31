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
LOCK_FILE = ROOT / "data" / "KEEPALIVE_LOCK"
CHECK_INTERVAL_S = 60
MISSES_BEFORE_RESTART = 2
HEALTH_URL = "http://127.0.0.1:7878/api/health"


def _pid_alive(pid: int) -> bool:
    """Windows stdlib pid-alive check (OpenProcess + STILL_ACTIVE)."""
    import ctypes
    if pid <= 0:
        return False
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001 — treat any probe failure as dead
        return False


def _server_up() -> bool:
    import httpx
    try:
        r = httpx.get(HEALTH_URL, timeout=5)
        return r.status_code == 200
    except Exception:  # noqa: BLE001 — any error = down
        return False


def _pids_from_netstat(output: str) -> set[str]:
    """Listener PIDs for our port from netstat -ano text (pure — tested)."""
    import re
    return {m.group(1) for m in re.finditer(
        r"TCP\s+127\.0\.0\.1:7878\s+\S+\s+LISTENING\s+(\d+)", output)}


def _kill_port_holder() -> None:
    """A HUNG server still owns the port — the replacement then dies with
    bind error 10048 and the system stays down (live 2026-08-31 04:20:
    the nightly vacuum + whisper warm blocked the loop ~2.5 min; the
    health checks timed out; keepalive's restart couldn't bind, and only
    the hang clearing itself saved the morning). Clear the port first:
    by the time keepalive has decided to restart, the holder is always
    our own broken server."""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=15).stdout
        for pid in _pids_from_netstat(out):
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001 — best effort; restart proceeds anyway
        pass


def _restart_server() -> None:
    _kill_port_holder()
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
    # SINGLETON (live 2026-08-31: the hermes runtime shim mirrors every
    # python child from an agent session — two keepalives spawned at once
    # would ping-pong taskkill each other's fresh servers during an
    # outage). A healthy instance holding the lock makes this one exit.
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
        except ValueError:
            old_pid = 0
        if old_pid and _pid_alive(old_pid):
            _log(f"keepalive already running (pid {old_pid}) — exiting")
            return
    LOCK_FILE.write_text(str(os.getpid()))
    # close the simultaneous-spawn race: if another instance wrote the
    # lock after our check, ITS pid is on disk now and this one exits
    if LOCK_FILE.read_text().strip() != str(os.getpid()):
        _log("keepalive lost the lock race — exiting")
        return
    _log("keepalive started (60s checks, restart after 2 misses)")
    misses = 0
    try:
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
    finally:
        LOCK_FILE.unlink(missing_ok=True)
    _log("keepalive stopped (stop flag)")


if __name__ == "__main__":
    main()
