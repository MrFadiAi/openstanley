"""Autopilot — the agent runs itself; the human only approves.

One scheduler job ticks every `autopilot_interval_min`; each tick runs ONE
phase, round-robin: study → create → engage → mentions → learn. Publish is
NEVER part of autopilot — approved content still ships only through the
human-gated publish loop. The engage and mentions phases draft replies only;
they may leave them approved+scheduled when `[agent] auto_approve_replies`
is true (default false), otherwise everything waits in the Inbox.

State lives in DB settings ("autopilot"): {enabled, last_tick, next_tick,
ticks, errors (last 5), phase}. Every tick is its own try/except — a failed
phase logs, joins the error ring, and the next tick continues.
"""
from __future__ import annotations

import random
import threading
from datetime import datetime, timedelta
from typing import Any

from ..core import db
from ..core.config import Config

PHASES: tuple[str, ...] = ("study", "create", "engage", "mentions", "learn")
ERROR_RING = 5                # errors kept in state
JITTER_MAX_S = 90             # tick fire-time jitter bound (human-like)
SETTING_KEY = "autopilot"

# one tick at a time — the scheduler job, a force-tick, and any straggler
# share this guard; a tick that fires while one runs is skipped, never queued
# (threading, not asyncio, so it works across loops and entry points).
_tick_running = threading.Lock()

DEFAULT_STATE: dict[str, Any] = {
    "enabled": False,
    "last_tick": None,
    "next_tick": None,
    "ticks": 0,
    "errors": [],
    "phase": None,
}


# ---------- state ----------

def get_state() -> dict[str, Any]:
    """Merged autopilot state (stored over defaults — survives restarts)."""
    stored = db.get_setting(SETTING_KEY)
    if not isinstance(stored, dict):
        stored = {}
    return {**DEFAULT_STATE, **stored}


def set_state(**fields: Any) -> dict[str, Any]:
    st = get_state()
    st.update(fields)
    db.set_setting(SETTING_KEY, st)
    return st


def set_enabled(enabled: bool) -> dict[str, Any]:
    return set_state(enabled=enabled)


# ---------- scheduling math (pure — tested without a scheduler) ----------

def next_phase(ticks: int) -> str:
    """Strict round-robin: tick #0 studies, #1 creates, #2 engages, #3 learns…"""
    return PHASES[ticks % len(PHASES)]


def jitter_seconds(rng: random.Random | None = None) -> float:
    r = rng or random
    return r.uniform(0, JITTER_MAX_S)


def compute_next_tick(now: datetime, interval_min: int) -> datetime:
    """Next fire time = interval + bounded jitter, so ticks never land
    on an exactly regular beat."""
    return now + timedelta(minutes=interval_min, seconds=jitter_seconds())


def interval_minutes(cfg: Config) -> int:
    return max(1, int(cfg.agent.autopilot_interval_min))


# ---------- phases ----------

def _max_draft_id() -> int:
    with db.connect() as c:
        row = c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM drafts").fetchone()
    return int(row["m"])


async def _phase_engage(agent, cfg: Config) -> dict:
    """Engage, autopilot-style: draft replies; auto-approve ONLY when the
    human opted in. Otherwise every reply stays a draft in the Inbox."""
    before = _max_draft_id()
    res = await agent.engage()
    if not cfg.agent.auto_approve_replies:
        res["approved_replies"] = 0
        db.log("autopilot", "engage: replies left as drafts (auto_approve off)")
        return res
    approved = 0
    for d in db.drafts_by_status("draft", 100):
        if d["id"] > before and d["kind"] == "reply" and d.get("scheduled_at"):
            db.update_draft(d["id"], status="approved")  # keeps proposed slot
            approved += 1
    res["approved_replies"] = approved
    if approved:
        db.log("autopilot", f"engage: auto-approved {approved} scheduled replies")
    return res


async def _phase_mentions(agent, cfg: Config) -> dict:
    """Mentions, autopilot-style: draft replies to people who talked to us;
    auto-approve ONLY when the human opted in (same gate as engage). A
    mention reply approved this way gets a near-term slot — conversation
    replies are worth more within the window."""
    before = _max_draft_id()
    res = await agent.mentions()
    if not cfg.agent.auto_approve_replies:
        res["approved_replies"] = 0
        db.log("autopilot", "mentions: replies left as drafts (auto_approve off)")
        return res
    approved = 0
    for d in db.drafts_by_status("draft", 100):
        if d["id"] > before and d["kind"] == "reply" \
                and (d.get("meta") or {}).get("source") == "mention":
            when = d.get("scheduled_at") or \
                (datetime.now() + timedelta(minutes=random.randint(3, 12))
                 ).isoformat(timespec="seconds")
            db.update_draft(d["id"], status="approved", scheduled_at=when)
            approved += 1
    res["approved_replies"] = approved
    if approved:
        db.log("autopilot", f"mentions: auto-approved {approved} replies")
    return res


async def run_tick(agent, cfg: Config) -> dict:
    """Run ONE phase (round-robin), update state, never raise.

    Publish is deliberately absent from PHASES — autopilot cannot ship.
    Skips (no counter advance, no error) if a previous tick is still running.
    """
    if not _tick_running.acquire(blocking=False):
        db.log("autopilot", "tick skipped — previous tick still running", level="warn")
        return {"ok": False, "phase": None, "result": None,
                "error": "tick skipped — previous tick still running",
                "ticks": get_state()["ticks"]}
    try:
        return await _run_tick_locked(agent, cfg)
    finally:
        _tick_running.release()


async def _run_tick_locked(agent, cfg: Config) -> dict:
    st = get_state()
    phase = next_phase(st["ticks"])
    now = datetime.now()
    ok, result, error = True, None, None
    db.log("autopilot", f"tick #{st['ticks'] + 1}: phase '{phase}'")
    try:
        if phase == "engage":
            result = await _phase_engage(agent, cfg)
        elif phase == "mentions":
            result = await _phase_mentions(agent, cfg)
        else:
            result = await getattr(agent, phase)()
    except Exception as e:  # noqa: BLE001 — one bad phase must not stop the loop
        ok, error = False, f"{phase}: {e}"
        db.log("autopilot", f"phase '{phase}' failed: {e}", level="error")
    errors = st["errors"]
    if error:
        errors = (errors + [error])[-ERROR_RING:]
    set_state(
        last_tick=now.isoformat(timespec="seconds"),
        next_tick=compute_next_tick(now, interval_minutes(cfg))
                  .isoformat(timespec="seconds"),
        ticks=st["ticks"] + 1,
        errors=errors,
        phase=phase,
    )
    return {"ok": ok, "phase": phase, "result": result, "error": error,
            "ticks": st["ticks"] + 1}
