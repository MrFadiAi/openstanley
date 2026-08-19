"""Smart slots v0.4.1 — signal math (metrics/spread/freshness), reply window,
roll-to-tomorrow, determinism, and the approve-path wiring.

Hermetic: per-test throwaway SQLite, no X reads (the picker only touches the
stored posts table), no LLM. The approve spy replaces pick_slot_with_reason,
and the static-parity test freezes server datetime.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"  # before importing the server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openstanley.core import db  # noqa: E402

import openstanley.server.__main__ as server  # noqa: E402  (also init_db on import)

client = TestClient(server.app)

from openstanley.core.config import Config  # noqa: E402
from openstanley.gen import slots  # noqa: E402

HANDLE = "slotuser"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Each test gets its own empty DB — signal inputs are exact."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "slots.db")
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _smart_on():
    """Flag back ON after every test (the settings test flips it globally)."""
    yield
    server.cfg.agent.smart_slots = True


def _seed_own(hour: int, *, likes: int, days_ago: int = 5, idx: int = 0) -> None:
    """One own post at a given local hour, N days back."""
    created = (datetime.now() - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    db.upsert_post({"x_id": f"slot-{hour}-{days_ago}-{idx}", "author_handle": HANDLE,
                    "is_own": 1, "created_at": created, "text": "seed",
                    "impressions": 0, "likes": likes, "reposts": 0, "replies": 0,
                    "bookmarks": 0})


def _seed_metrics_corpus() -> None:
    """>=20 own posts with avg engagement peaking 13 > 18 > 9 — forces the
    metrics signal (source 'real') with base scores 1.0 / 0.5 / 0.1."""
    for i in range(8):
        _seed_own(13, likes=10, days_ago=10, idx=i)
    for i in range(8):
        _seed_own(18, likes=5, days_ago=9, idx=i)
    for i in range(5):
        _seed_own(9, likes=1, days_ago=8, idx=i)


def _at(h: int, m: int = 0) -> datetime:
    """A `now` pinned to today's clock — deterministic occurrences."""
    return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)


# ---------------- signal math ----------------

def test_heuristic_below_20_posts():
    """Thin data → post_times ARE the candidates; top pick is the next slot."""
    cfg = Config()
    now = _at(10, 30)
    ranked = slots.best_slots(cfg, now)
    assert slots.source(cfg) == "heuristic"
    assert sorted(s["hour"] for s in ranked) == [9, 13, 18]
    top = ranked[0]
    assert top["hour"] == 13 and top["at"].date() == now.date()
    assert top["reason"].startswith("cadence slot 13:00")
    assert "no recent post" in top["reason"]


def test_metrics_override_at_20_posts():
    """>=20 posts → real engagement-by-hour picks the peak, not the cadence."""
    _seed_metrics_corpus()
    cfg = Config()
    ranked = slots.best_slots(cfg, _at(7, 0))
    assert slots.source(cfg) == "real"
    assert ranked[0]["hour"] == 13
    assert ranked[0]["reason"].startswith("metrics peak 13:00")
    # top-k counts occurrences, so the peak hour may legitimately appear twice
    # (today + tomorrow); the invariant that matters is that the peak hour
    # (base 1.0) outranks the runner-up hour (base 0.5) at EVERY occurrence
    everything = slots.best_slots(cfg, _at(7, 0), k=64)
    s13 = [s["score"] for s in everything if s["hour"] == 13]
    s18 = [s["score"] for s in everything if s["hour"] == 18]
    s9 = [s["score"] for s in everything if s["hour"] == 9]
    assert min(s13) > max(s18) > max(s9)


def test_spread_penalty():
    """A slot inside 2h of the last published post loses to a well-spread one
    even though it is fresher."""
    cfg = Config()
    now = _at(8, 30)
    last = now - timedelta(hours=1)  # 07:30 today
    created = last.isoformat(timespec="seconds")
    db.upsert_post({"x_id": "slot-last", "author_handle": HANDLE, "is_own": 1,
                    "created_at": created, "text": "recent", "impressions": 0,
                    "likes": 0, "reposts": 0, "replies": 0, "bookmarks": 0})
    ranked = slots.best_slots(cfg, now)
    by_hour = {s["hour"]: s for s in ranked if s["at"].date() == now.date()}
    assert by_hour[9]["score"] < by_hour[13]["score"], \
        "2h-spread penalty must outweigh 09:00's freshness edge"
    assert ranked[0]["hour"] == 13
    assert "after last post" in ranked[0]["reason"]


def test_past_slot_rolls_to_tomorrow():
    """All of today's slots already started → the pick is tomorrow's first."""
    cfg = Config()
    now = _at(20, 0)
    top = slots.best_slots(cfg, now)[0]
    assert top["at"].date() == (now + timedelta(days=1)).date()
    assert top["hour"] == 9
    picked = slots.pick_slot(cfg, "post", now)
    assert picked == top["at"]


# ---------------- reply window vs post behavior ----------------

def test_reply_rides_nearest_slot_inside_90min():
    """17:30 → 18:00 is inside the conversation window: the reply takes it
    even though the scored best (13:00 tomorrow) is hours better."""
    _seed_metrics_corpus()
    cfg = Config()
    now = _at(17, 30)
    reply_at = slots.pick_slot(cfg, "reply", now)
    post_at = slots.pick_slot(cfg, "post", now)
    assert reply_at.hour == 18 and reply_at.date() == now.date(), \
        "reply must take the nearest slot within 90 min"
    assert post_at.hour == 13 and post_at.date() == (now + timedelta(days=1)).date(), \
        "post waits for the metrics peak instead"
    assert reply_at != post_at


def test_reply_falls_back_to_best_when_window_empty():
    """19:30 → nothing inside 90 min: the reply rides best_slots[0] like a post."""
    cfg = Config()
    now = _at(19, 30)
    assert slots.pick_slot(cfg, "reply", now) == slots.pick_slot(cfg, "post", now)
    assert slots.pick_slot(cfg, "reply", now).hour == 9


def test_determinism():
    """Same (cfg, now, db state) → identical ranking and pick, twice."""
    _seed_metrics_corpus()
    cfg = Config()
    now = _at(11, 0)
    a, b = slots.best_slots(cfg, now), slots.best_slots(cfg, now)
    assert a == b
    assert (len(a) == 3
            and a[0]["score"] >= a[1]["score"] >= a[2]["score"]
            and all("reason" in s and "at" in s for s in a))
    assert slots.pick_slot(cfg, "post", now) == slots.pick_slot(cfg, "post", now)


# ---------------- approve-path wiring ----------------

def test_approve_uses_pick_slot_with_reason(monkeypatch):
    """Slotless drafts: approve schedules via the smart picker and persists
    the reason into meta (spy proves the call, hermetic — no real scoring)."""
    picked = _at(15, 0) + timedelta(days=1)
    reason = "metrics peak 13:00 · 5h after last post"
    calls: list[str] = []

    def _spy(cfg, kind, now):
        calls.append(kind)
        return picked, reason

    monkeypatch.setattr(slots, "pick_slot_with_reason", _spy)

    did = db.add_draft(text="smart slot approve spy post")
    r = client.post(f"/api/drafts/{did}/approve", json={}).json()
    assert r["scheduled_at"] == picked.isoformat(timespec="seconds")
    assert r["scheduled_reason"] == reason
    assert calls == ["post"]
    d = db.get_draft(did)
    assert d["scheduled_at"] == picked.isoformat(timespec="seconds")
    assert d["meta"]["scheduled_reason"] == reason

    # replies route through the reply branch of the same picker
    did2 = db.add_draft(text="smart slot approve spy reply", kind="reply",
                        meta={"reply_to_x_id": "target-1"})
    assert client.post(f"/api/drafts/{did2}/approve", json={}).json()["scheduled_reason"] == reason
    assert calls == ["post", "reply"]


def test_config_off_static_behavior_identical(monkeypatch):
    """smart_slots=False → approve is the v0.3 static cadence, bit-identical,
    and never touches the picker."""
    monkeypatch.setattr(server.cfg.agent, "smart_slots", False)

    def _bomb(*a, **k):  # noqa: ANN001 — must never run
        raise AssertionError("pick_slot must not run when smart_slots is off")

    monkeypatch.setattr(slots, "pick_slot_with_reason", _bomb)

    frozen = datetime.now().replace(hour=10, minute=30, second=0, microsecond=0)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN102
            return frozen

    monkeypatch.setattr(server, "datetime", _FrozenDateTime)
    expected = server._next_slot()

    did = db.add_draft(text="static parity post")
    r = client.post(f"/api/drafts/{did}/approve", json={}).json()
    assert r["scheduled_at"] == expected, "flag off must reproduce _next_slot exactly"
    assert r["scheduled_reason"] is None
    assert "scheduled_reason" not in (db.get_draft(did)["meta"] or {})


# ---------------- endpoints + settings ----------------

def test_calendar_smart_and_times_reasons():
    """Calendar carries per-day scored chips; analytics/times carries the same
    reason strings the scheduler logs (single source)."""
    _seed_metrics_corpus()
    cal = client.get("/api/calendar").json()
    assert cal["smart"]["enabled"] is True
    assert cal["smart"]["source"] == "real"
    chips = next(iter(cal["smart"]["slots"].values()))
    assert chips and {"time", "score", "reason"} <= set(chips[0])
    assert any(c["reason"].startswith("metrics peak") for c in chips)

    times = client.get("/api/analytics/times").json()
    assert "reasons" in times and times["reasons"]
    assert times["reasons"]["13"].startswith("metrics peak 13:00")

    server.cfg.agent.smart_slots = False
    assert client.get("/api/calendar").json()["smart"]["enabled"] is False


def test_smart_slots_settings_roundtrip():
    """POST /api/settings flips the live flag; GET reflects it."""
    r = client.post("/api/settings", json={"smart_slots": False}).json()
    assert r["smart_slots"] is False
    assert server.cfg.agent.smart_slots is False
    assert client.get("/api/settings").json()["smart_slots"] is False
    client.post("/api/settings", json={"smart_slots": True})
    assert server.cfg.agent.smart_slots is True
