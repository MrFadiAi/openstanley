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
