"""Autopilot v0.3.5 — self-driving agent, hermetic (dryrun X, fake agent).

Covers: tick round-robin + jitter bounds, state transitions, the 5-slot
error ring, the engage auto-approve gate, publish-never-called, and the
API endpoints (start/stop/force-tick/interval persistence).
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"  # no cron loop inside tests

from openstanley.core import db                                   # noqa: E402
db.init_db()

from fastapi.testclient import TestClient                      # noqa: E402

from openstanley.core.config import Config                        # noqa: E402
from openstanley.gen import autopilot as ap                       # noqa: E402
import openstanley.server.__main__ as server                      # noqa: E402


def _reset():
    db.set_setting(ap.SETTING_KEY, None)  # back to DEFAULT_STATE
    db.set_setting("agent_autopilot_interval_min", None)
    server.cfg.agent.autopilot_interval_min = 45  # undo in-process config leak


class FakeAgent:
    """Records phase calls; publish raises if autopilot ever dares to ship."""

    def __init__(self, fail_on: set[str] | None = None):
        self.calls: list[str] = []
        self.fail_on = fail_on or set()

    async def study(self):
        self.calls.append("study")
        if "study" in self.fail_on:
            raise RuntimeError("study exploded")
        return {"niche_new": 0, "bank": 12}

    async def create(self):
        self.calls.append("create")
        if "create" in self.fail_on:
            raise RuntimeError("create exploded")
        return {"drafts": 2}

    async def learn(self):
        self.calls.append("learn")
        if "learn" in self.fail_on:
            raise RuntimeError("learn exploded")
        return {"refreshed": 0}

    async def engage(self):
        self.calls.append("engage")
        if "engage" in self.fail_on:
            raise RuntimeError("engage exploded")
        return {"new_mentions": 0, "replies_drafted": 0,
                "niche_replies_scheduled": 0}

    async def mentions(self):
        self.calls.append("mentions")
        if "mentions" in self.fail_on:
            raise RuntimeError("mentions exploded")
        return {"mentions_new": 0, "replies_drafted": 0}

    async def publish(self):
        self.calls.append("publish")
        raise AssertionError("autopilot must NEVER call publish")


# ---------------- module: round-robin + jitter + state ----------------

def _run(agent, cfg):
    import asyncio
    return asyncio.run(ap.run_tick(agent, cfg))


def test_tick_round_robin_order():
    _reset()
    agent = FakeAgent()
    cfg = Config()
    phases = []
    for _ in range(10):
        res = _run(agent, cfg)
        assert res["ok"], res
        phases.append(res["phase"])
    assert phases == ["study", "create", "engage", "mentions", "learn"] * 2
    assert "publish" not in agent.calls
    st = ap.get_state()
    assert st["ticks"] == 10 and st["phase"] == "learn" and st["errors"] == []
    assert st["last_tick"] and st["next_tick"]


def test_next_phase_order():
    assert [ap.next_phase(i) for i in range(10)] == [
        "study", "create", "engage", "mentions", "learn", "study",
        "create", "engage", "mentions", "learn"]


def test_jitter_bounds():
    now = datetime(2026, 8, 19, 12, 0, 0)
    base = timedelta(minutes=45)
    for _ in range(200):
        nxt = ap.compute_next_tick(now, 45)
        delay = nxt - now
        assert base <= delay <= base + timedelta(seconds=ap.JITTER_MAX_S), delay
    # deterministic rng path too
    rng = random.Random(1)
    for _ in range(50):
        assert 0 <= ap.jitter_seconds(rng) <= ap.JITTER_MAX_S


def test_state_transitions_and_defaults():
    _reset()
    st = ap.get_state()
    assert st == {"enabled": False, "last_tick": None, "next_tick": None,
                  "ticks": 0, "errors": [], "phase": None}
    ap.set_enabled(True)
    assert ap.get_state()["enabled"] is True
    ap.set_enabled(False)
    assert ap.get_state()["enabled"] is False


def test_error_ring_keeps_last_5_and_loop_continues():
    _reset()
    agent = FakeAgent(fail_on={"study", "create", "engage", "mentions", "learn"})
    cfg = Config()
    for i in range(7):
        res = _run(agent, cfg)
        assert res["ok"] is False and res["error"], res
    st = ap.get_state()
    assert len(st["errors"]) == 5, "ring must cap at 5"
    # ticks ran study,create,engage,mentions,learn,study,create — the first
    # two errors (study, create) are dropped; the ring keeps the last five
    assert [e.split(":")[0] for e in st["errors"]] == \
        ["engage", "mentions", "learn", "study", "create"]
    assert st["ticks"] == 7, "failed ticks still advance the pointer"
    # and the next tick still runs (loop never halts): ticks=7 → phase engage
    agent.fail_on = set()
    res = _run(agent, cfg)
    assert res["ok"] and res["phase"] == "engage"


def test_publish_never_called_across_all_phases():
    _reset()
    agent = FakeAgent()  # publish raises AssertionError if invoked
    for _ in range(5):  # one full rotation
        _run(agent, Config())
    assert agent.calls.count("publish") == 0


def test_overlapping_tick_is_skipped_not_queued():
    """Scheduler tick vs force-tick overlap: second call skips, counter stays."""
    _reset()
    import asyncio
    release = asyncio.Event()
    cfg = Config()

    class BlockingAgent(FakeAgent):
        async def study(self):
            await super().study()
            await release.wait()  # park the first tick mid-phase

    agent = BlockingAgent()

    async def scenario():
        first = asyncio.create_task(ap.run_tick(agent, cfg))
        await asyncio.sleep(0.05)  # first tick is now parked inside study
        second = await ap.run_tick(agent, cfg)
        release.set()
        return second, await first

    second, first = asyncio.run(scenario())
    assert first["ok"] and first["phase"] == "study"
    assert second["ok"] is False and "skipped" in second["error"]
    st = ap.get_state()
    assert st["ticks"] == 1, "skipped tick must not advance the pointer"
    _reset()


# ---------------- engage approval gate ----------------

def _seed_scheduled_reply(text: str = " autopilot gate test reply") -> int:
    when = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
    return db.add_draft(text=text, kind="reply", scheduled_at=when,
                        meta={"reply_to_x_id": "gate-target-1", "source": "test"})


class ReplyingAgent(FakeAgent):
    """engage() that actually creates a scheduled reply draft (like the real one)."""

    async def engage(self):
        await super().engage()
        self.reply_id = _seed_scheduled_reply()
        return {"new_mentions": 0, "replies_drafted": 0,
                "niche_replies_scheduled": 1}


def _run_engage(agent, cfg):
    # rotate state until engage is next, then tick once
    st = ap.get_state()
    while ap.next_phase(st["ticks"]) != "engage":
        _run(FakeAgent(), cfg)  # burn phases with a clean agent
        st = ap.get_state()
    return _run(agent, cfg)


def test_engage_autoapprove_off_leaves_zero_approved():
    _reset()
    cfg = Config()
    cfg.agent.auto_approve_replies = False  # default
    agent = ReplyingAgent()
    res = _run_engage(agent, cfg)
    assert res["ok"] and res["phase"] == "engage"
    assert res["result"]["approved_replies"] == 0
    d = db.get_draft(agent.reply_id)
    assert d["status"] == "draft", "reply must wait in the Inbox"
    assert d["scheduled_at"], "proposed slot kept"
    db.update_draft(agent.reply_id, status="rejected")  # cleanup


def test_engage_autoapprove_on_leaves_reply_approved_scheduled():
    _reset()
    cfg = Config()
    cfg.agent.auto_approve_replies = True
    agent = ReplyingAgent()
    res = _run_engage(agent, cfg)
    assert res["ok"] and res["result"]["approved_replies"] == 1
    d = db.get_draft(agent.reply_id)
    assert d["status"] == "approved" and d["scheduled_at"]
    db.update_draft(agent.reply_id, status="rejected")  # cleanup


# ---------------- API ----------------

client = TestClient(server.app)


def test_api_autopilot_start_stop_and_interval():
    _reset()
    try:
        r = client.get("/api/autopilot")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False and body["ticks"] == 0
        assert body["interval_min"] == 45 and body["job_active"] is False

        r = client.post("/api/autopilot", json={"enabled": True, "interval_min": 30})
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True and body["interval_min"] == 30
        # interval persisted for restarts
        assert db.get_setting("agent_autopilot_interval_min") == 30

        assert client.post("/api/autopilot", json={"enabled": False}).status_code == 200
        assert client.get("/api/autopilot").json()["enabled"] is False
        # invalid interval rejected
        assert client.post("/api/autopilot",
                           json={"enabled": True, "interval_min": 0}).status_code == 400
    finally:
        _reset()  # never leave enabled=true behind, even on failure


def test_api_force_tick_runs_phase_and_updates_state():
    _reset()
    calls = []

    async def fake_study():
        calls.append("study")
        return {"niche_new": 1, "bank": 3}

    server.agent.study = fake_study  # instance attr shadows the method
    try:
        r = client.post("/api/autopilot/tick")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["phase"] == "study"
        assert calls == ["study"]
        assert body["state"]["ticks"] == 1
        assert body["state"]["last_tick"] and body["state"]["phase"] == "study"
    finally:
        del server.agent.study  # restore the real bound method
        _reset()
