"""Chat watchdog — LLM outages, tool failure storms, draft bursts get
noticed, alerted, and (for bursts) stopped."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"

import pytest  # noqa: E402

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.system import watchdog as wd                      # noqa: E402


@pytest.fixture(autouse=True)
def _no_tg_alerts(monkeypatch):
    """Alerts are recorded in state; the TG delivery path is faked so no
    test can ever touch api.telegram.org."""
    import openstanley.integrations.telegram as tg
    monkeypatch.setattr(tg, "is_enabled", lambda: False)


def _stale_window() -> None:
    """Age every saved draft timestamp out of the 1h burst window."""
    st = db.get_setting("watchdog") or {}
    old = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    st["chat_draft_times"] = [old] * len(st.get("chat_draft_times", []))
    db.set_setting("watchdog", st)


def test_chat_llm_three_consecutive_failures_trip():
    wd.note_chat_llm(False, "timeout")
    wd.note_chat_llm(False, "timeout")
    assert not wd.status()["chat_llm"]["degraded"], "2 is below the trip"
    wd.note_chat_llm(False, "500 from provider")
    s = wd.status()["chat_llm"]
    assert s["degraded"] and s["consecutive_failures"] == 3
    assert any("chat LLM failed" in a for a in wd.status()["alerts"])


def test_chat_llm_success_resets_and_rearms():
    for _ in range(4):
        wd.note_chat_llm(False, "down")
    assert wd.status()["chat_llm"]["degraded"]
    wd.note_chat_llm(True)
    s = wd.status()["chat_llm"]
    assert not s["degraded"] and s["consecutive_failures"] == 0
    # the alert fired ONCE for the episode, not per failure
    llm_alerts = [a for a in wd.status()["alerts"] if "chat LLM failed" in a]
    assert len(llm_alerts) == 1


def test_tool_failure_rate_trips_once_per_episode():
    for i in range(10):
        wd.note_tool(i >= 6)  # 6 failures (ok=False) in 10 calls
    s = wd.status()["tools"]
    assert s["recent_calls"] == 10 and s["recent_failures"] == 6
    assert s["alerted"] and any("tool calls" in a for a in wd.status()["alerts"])
    n_alerts = len([a for a in wd.status()["alerts"] if "tool calls" in a])
    wd.note_tool(False)  # still broken — must not re-alert
    wd.note_tool(False)
    assert len([a for a in wd.status()["alerts"] if "tool calls" in a]) == n_alerts
    for _ in range(12):  # recovery re-arms
        wd.note_tool(True)
    assert not wd.status()["tools"]["alerted"]


def test_tool_rate_below_minimum_never_trips():
    for i in range(9):
        wd.note_tool(i >= 6)  # would be >50% but under TOOL_MIN=10
    assert not wd.status()["tools"]["alerted"]


def test_chat_draft_burst_blocks_then_reopens():
    # saves 1..6 land; the 7th inside the window is blocked
    for i in range(6):
        assert wd.allow_chat_draft(), f"save {i + 1} must be allowed"
        wd.note_chat_draft()
    assert wd.status()["chat_drafts"]["last_hour"] == 6
    assert not wd.allow_chat_draft(), "7th save inside the window is blocked"
    assert wd.status()["chat_drafts"]["blocked"]
    assert any("runaway" in a or "blocked" in a for a in wd.status()["alerts"])
    # window drains → saving re-opens
    _stale_window()
    assert wd.allow_chat_draft()
    assert not wd.status()["chat_drafts"]["blocked"]


def test_burst_alert_fires_once_per_episode():
    for _ in range(6):
        if wd.allow_chat_draft():
            wd.note_chat_draft()
    # the 7th check trips the block AND fires the alert
    assert not wd.allow_chat_draft()
    n = len([a for a in wd.status()["alerts"] if "chat-born drafts" in a])
    assert n == 1
    assert not wd.allow_chat_draft()  # still blocked, still one alert
    assert len([a for a in wd.status()["alerts"] if "chat-born drafts" in a]) == 1


def test_tg_handler_streak_trips_at_ten():
    for _ in range(9):
        wd.note_tg_handler(False, "boom")
    assert wd.status()["telegram"]["consecutive_handler_failures"] == 9
    assert not any("TG handler" in a for a in wd.status()["alerts"])
    wd.note_tg_handler(False, "boom")
    assert any("TG handler" in a for a in wd.status()["alerts"])
    wd.note_tg_handler(True)
    assert wd.status()["telegram"]["consecutive_handler_failures"] == 0


def test_alert_delivery_failure_never_raises(monkeypatch):
    import openstanley.integrations.telegram as tg
    monkeypatch.setattr(tg, "is_enabled", lambda: True)

    def _boom(text):  # noqa: ANN001
        raise RuntimeError("tg down")
    monkeypatch.setattr(tg, "notify_bg", _boom)
    for _ in range(3):
        wd.note_chat_llm(False, "x")
    assert wd.status()["chat_llm"]["degraded"], \
        "the monitored path survived the broken alert channel"


def test_health_line_renders_both_states():
    assert "chat ok" in wd.health_line()
    for _ in range(3):
        wd.note_chat_llm(False, "down")
    line = wd.health_line()
    assert "DOWN" in line


def test_draft_from_chat_respects_guard(monkeypatch):
    from openstanley.core.config import Config
    from openstanley.gen import chat as chat_mod
    cfg = Config()
    saved = []
    monkeypatch.setattr(db, "add_draft",
                        lambda *a, **k: saved.append(1) or 123)
    for _ in range(6):
        if wd.allow_chat_draft():
            wd.note_chat_draft()
    did = chat_mod.draft_from_chat(cfg, "a perfectly good post text here")
    assert did == -1 and saved == [], "blocked save never reaches the DB"


def test_watchdog_state_survives_restart():
    for _ in range(3):
        wd.note_chat_llm(False, "down")
    # fresh import cycle is the same DB setting — simulate a restart by
    # reading state through a new status() call after process-level reload
    st = db.get_setting("watchdog")
    assert st["chat_llm_degraded"] is True
    assert wd.status()["chat_llm"]["degraded"] is True


def test_user_turn_resets_burst_guard():
    """Live 2026-09-01 19:41: the owner's interactive session had drafts
    blocked by saves from earlier the same hour. A fresh user message
    proves the owner is present — the runaway guard resets; a true burst
    (no user turns) still blocks."""
    from openstanley.system import watchdog as wd
    for _ in range(6):
        wd.note_chat_draft()
    assert wd.allow_chat_draft() is False      # burst trips
    wd.note_user_turn()                         # owner speaks
    assert wd.allow_chat_draft() is True        # reset
    for _ in range(6):
        wd.note_chat_draft()
    assert wd.allow_chat_draft() is False       # runaway still caught
