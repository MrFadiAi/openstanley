"""Chat watchdog — the monitor over the chat pipeline's health.

History this exists to prevent repeating: an LLM outage showed only as
one-off "(LLM error)" bubbles nobody saw; one confused reply spawned 9
drafts (the runaway candidate storm); TG handler errors degrade the bot
quietly. The watchdog counts what those incidents looked like from the
inside and (a) tells the owner on Telegram, (b) where a lever exists,
pulls it — chat-born draft saves are refused while a burst is trip-wired.

Design contract: `note()`/`allow_chat_draft()` NEVER raise into monitored
code — a broken watchdog must not become the outage. State is one DB
setting (survives restarts); counters are approximate under concurrency by
design (read-modify-write under one small lock), which is plenty for
threshold watching.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Optional

from ..core import db

SETTING_KEY = "watchdog"

CHAT_LLM_TRIP = 3          # consecutive chat LLM failures → degraded + alert
TOOL_RING = 20             # tool outcomes remembered
TOOL_MIN = 10              # …minimum before the rate is judged
TOOL_FAIL_RATE = 0.5       # …failure rate that trips
CHAT_DRAFT_WINDOW_H = 1    # chat-draft burst window
CHAT_DRAFT_BURST = 6       # chat-born drafts inside the window → block more
TG_HANDLER_TRIP = 10       # consecutive TG handler errors → alert
ALERT_RING = 5             # alerts kept in state for /status

DEFAULT_STATE: dict[str, Any] = {
    "chat_llm_consecutive": 0,
    "chat_llm_degraded": False,
    "chat_llm_last_error": None,
    "tool_ring": [],
    "tool_alerted": False,
    "chat_draft_times": [],
    "chat_draft_blocked": False,
    "tg_handler_consecutive": 0,
    "alerts": [],
}

_lock = threading.Lock()


def _state() -> dict[str, Any]:
    stored = db.get_setting(SETTING_KEY)
    if not isinstance(stored, dict):
        stored = {}
    return {**DEFAULT_STATE, **stored}


def _save(st: dict[str, Any]) -> None:
    db.set_setting(SETTING_KEY, st)


def _alert(st: dict[str, Any], key: str, message: str) -> None:
    """Record + broadcast one alert. Best-effort by contract."""
    line = f"{datetime.now().isoformat(timespec='seconds')} · {message}"
    st["alerts"] = (st["alerts"] + [line])[-ALERT_RING:]
    db.log("watchdog", message, level="error")
    try:
        from ..integrations import telegram as tg_mod
        if tg_mod.is_enabled():
            tg_mod.notify_bg(f"⚠️ Watchdog: {message}")
    except Exception as e:  # noqa: BLE001 — never break the monitored path
        db.log("watchdog", f"alert delivery failed: {e}", level="warn")


def note_chat_llm(ok: bool, detail: str = "") -> None:
    """Chat LLM outcome. 3 consecutive failures → degraded + one alert;
    the first success clears the flag (and re-arms the alert)."""
    with _lock:
        st = _state()
        if ok:
            st["chat_llm_consecutive"] = 0
            if st["chat_llm_degraded"]:
                st["chat_llm_degraded"] = False
                db.log("watchdog", "chat LLM recovered")
            _save(st)
            return
        st["chat_llm_consecutive"] += 1
        st["chat_llm_last_error"] = detail[:200]
        if st["chat_llm_consecutive"] >= CHAT_LLM_TRIP and not st["chat_llm_degraded"]:
            st["chat_llm_degraded"] = True
            _alert(st, "chat_llm",
                   f"chat LLM failed {st['chat_llm_consecutive']}× in a row "
                   f"({detail[:120] or 'unknown error'}) — replies are down, "
                   "scheduled drafts keep flowing")
        _save(st)


def note_tool(ok: bool) -> None:
    """Tool outcome → rolling ring. >50% failures over the last >=10 calls
    alerts once per episode (re-arms when the rate recovers)."""
    with _lock:
        st = _state()
        st["tool_ring"] = (st["tool_ring"] + [bool(ok)])[-TOOL_RING:]
        ring = st["tool_ring"]
        if len(ring) >= TOOL_MIN:
            rate = 1.0 - sum(ring) / len(ring)
            if rate > TOOL_FAIL_RATE and not st["tool_alerted"]:
                st["tool_alerted"] = True
                _alert(st, "tools",
                       f"{int(rate * len(ring))}/{len(ring)} recent tool calls "
                       "failed — chat actions are erroring out")
            elif rate <= TOOL_FAIL_RATE:
                st["tool_alerted"] = False
        _save(st)


def note_tg_handler(ok: bool, detail: str = "") -> None:
    """TG handler outcome. 10 consecutive errors → one alert; success
    re-arms. getUpdates network blips already back off in the poll loop —
    this watches the handlers themselves (the reply path)."""
    with _lock:
        st = _state()
        if ok:
            if st["tg_handler_consecutive"] >= TG_HANDLER_TRIP:
                db.log("watchdog", "TG handlers recovered")
            st["tg_handler_consecutive"] = 0
            _save(st)
            return
        st["tg_handler_consecutive"] += 1
        if st["tg_handler_consecutive"] == TG_HANDLER_TRIP:
            _alert(st, "tg_handler",
                   f"{TG_HANDLER_TRIP} consecutive TG handler failures "
                   f"({detail[:120] or 'see telegram log'}) — replies may be "
                   "silently not landing")
        _save(st)


def allow_chat_draft() -> bool:
    """Burst guard: True while chat-born draft saves are allowed. Blocks
    once CHAT_DRAFT_BURST landed inside the window; re-opens when the
    window drains past the oldest burst save."""
    with _lock:
        st = _state()
        cutoff = datetime.now() - timedelta(hours=CHAT_DRAFT_WINDOW_H)
        times = [t for t in st["chat_draft_times"] if _parse(t) > cutoff]
        if len(times) >= CHAT_DRAFT_BURST:
            if not st["chat_draft_blocked"]:
                st["chat_draft_blocked"] = True
                _alert(st, "chat_draft",
                       f"{len(times)} chat-born drafts in the last hour — "
                       "saving MORE is blocked until the burst drains "
                       "(runaway guard)")
                _save(st)
            return False
        if st["chat_draft_blocked"]:
            st["chat_draft_blocked"] = False
            db.log("watchdog", "chat draft burst drained — saving re-opened")
        st["chat_draft_times"] = times
        _save(st)
        return True


def note_user_turn() -> None:
    """A fresh inbound user message: the OWNER is present and asking.
    Resets the chat-draft burst window — the guard exists to stop a
    RUNAWAY agent (the 4-duplicate incident), and a human actively
    requesting drafts is the opposite of a runaway. Live 2026-09-01
    19:41: the owner's interactive long-post session got its drafts
    blocked by saves from earlier in the same hour."""
    with _lock:
        st = _state()
        st["chat_draft_times"] = []
        if st.get("chat_draft_blocked"):
            st["chat_draft_blocked"] = False
            db.log("watchdog", "owner active — chat draft burst reset")
        _save(st)


def note_chat_draft() -> None:
    """A chat-born draft actually saved — feed the burst window."""
    with _lock:
        st = _state()
        st["chat_draft_times"] = (
            st["chat_draft_times"] +
            [datetime.now().isoformat(timespec="seconds")])[-50:]
        _save(st)


def _parse(iso: Optional[str]) -> datetime:
    try:
        return datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return datetime.min


def status() -> dict[str, Any]:
    """Watchdog view for /api/watchdog + the TG /status line."""
    with _lock:
        st = _state()
    cutoff = datetime.now() - timedelta(hours=CHAT_DRAFT_WINDOW_H)
    return {
        "chat_llm": {
            "degraded": st["chat_llm_degraded"],
            "consecutive_failures": st["chat_llm_consecutive"],
            "last_error": st["chat_llm_last_error"],
        },
        "tools": {
            "recent_calls": len(st["tool_ring"]),
            "recent_failures": len(st["tool_ring"]) - sum(st["tool_ring"]),
            "alerted": st["tool_alerted"],
        },
        "chat_drafts": {
            "last_hour": sum(1 for t in st["chat_draft_times"]
                             if _parse(t) > cutoff),
            "blocked": st["chat_draft_blocked"],
            "burst_limit": CHAT_DRAFT_BURST,
        },
        "telegram": {
            "consecutive_handler_failures": st["tg_handler_consecutive"],
        },
        "alerts": st["alerts"],
    }


def health_line() -> str:
    """One compact line for the TG /status command."""
    s = status()
    bits = []
    if s["chat_llm"]["degraded"]:
        bits.append(f"chat LLM DOWN ({s['chat_llm']['consecutive_failures']} in a row)")
    else:
        bits.append("chat ok")
    t = s["tools"]
    if t["recent_calls"] >= TOOL_MIN:
        bits.append(f"tools {t['recent_calls'] - t['recent_failures']}/{t['recent_calls']} ok"
                    + (" ⚠" if t["alerted"] else ""))
    d = s["chat_drafts"]
    if d["blocked"]:
        bits.append(f"chat drafts BLOCKED ({d['last_hour']} last hour)")
    elif d["last_hour"]:
        bits.append(f"chat drafts {d['last_hour']}/h")
    if s["telegram"]["consecutive_handler_failures"] >= TG_HANDLER_TRIP:
        bits.append("TG handlers failing")
    return "watchdog: " + ", ".join(bits)
