"""Keepalive port-clearing — a hung server must not trap the replacement."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_pids_from_netstat_finds_listener():
    from scripts.keepalive import _pids_from_netstat
    sample = (
        "  TCP    127.0.0.1:7878         0.0.0.0:0              LISTENING       23036\n"
        "  TCP    127.0.0.1:5173         0.0.0.0:0              LISTENING       999\n"
        "  TCP    127.0.0.1:7878         127.0.0.1:55555        ESTABLISHED     23036\n"
    )
    assert _pids_from_netstat(sample) == {"23036"}


def test_pids_from_netstat_empty_on_no_listener():
    from scripts.keepalive import _pids_from_netstat
    assert _pids_from_netstat("  TCP    0.0.0.0:0 ...\n") == set()


def test_pid_alive_self_and_bogus():
    import os
    from scripts.keepalive import _pid_alive
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(999999999) is False
    assert _pid_alive(0) is False


def test_singleton_lock_blocks_second_instance(tmp_path, monkeypatch):
    """A live pid in the lock makes main() exit before any loop runs."""
    import os
    from scripts import keepalive as ka
    monkeypatch.setattr(ka, "LOCK_FILE", tmp_path / "KEEPALIVE_LOCK")
    monkeypatch.setattr(ka, "STOP_FILE", tmp_path / "KEEPALIVE_STOP")
    (tmp_path / "KEEPALIVE_LOCK").write_text(str(os.getpid()))  # we are alive
    logged = []
    monkeypatch.setattr(ka, "_log", lambda m: logged.append(m))
    ka.main()  # must return immediately, not loop
    assert any("already running" in m for m in logged), logged
    assert not (tmp_path / "KEEPALIVE_STOP").exists()
