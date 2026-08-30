"""Agentic tier — trend watches, thread continuation, winner-quote.

Trend watches: standing "tell me when X trends" — the agent checks each
watch hourly and alerts when the topic surges on X.
Thread continuation: reply to your own yesterday's thread while it's warm.
Winner-quote: when a post crosses 2x baseline, auto-draft a quote follow-up.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from ..core import db
from ..core.config import Config

WATCH_KEY = "trend_watches"       # list of {topic, created, last_alert}
WATCH_COOLDOWN_H = 6              # per-topic alert cooldown
TREND_HIT_MIN = 3                 # mentions needed to alert


def add_watch(topic: str) -> dict:
    """Register a standing trend watch."""
    topic = topic.strip()
    if not topic:
        return {"ok": False, "error": "topic required"}
    watches = db.get_setting(WATCH_KEY) or []
    if any(w["topic"].lower() == topic.lower() for w in watches):
        return {"ok": False, "error": f"already watching '{topic}'"}
    watches.append({"topic": topic,
                    "created": datetime.now().isoformat(timespec="seconds"),
                    "last_alert": None})
    db.set_setting(WATCH_KEY, watches)
    db.log("watch", f"trend watch added: '{topic}'")
    return {"ok": True, "topic": topic, "total_watches": len(watches)}


def list_watches() -> list[dict]:
    return db.get_setting(WATCH_KEY) or []


def remove_watch(topic: str) -> dict:
    watches = db.get_setting(WATCH_KEY) or []
    kept = [w for w in watches if w["topic"].lower() != topic.strip().lower()]
    if len(kept) == len(watches):
        return {"ok": False, "error": f"no watch on '{topic}'"}
    db.set_setting(WATCH_KEY, kept)
    return {"ok": True, "removed": topic, "remaining": len(kept)}


async def check_watches(cfg: Config) -> dict:
    """One pass over every watch: search X for the topic, alert when it
    surges. Cooldown per topic prevents spam."""
    from ..integrations import telegram as tg
    from . import websearch
    watches = list_watches()
    if not watches:
        return {"checked": 0}
    alerted = 0
    now = datetime.now()
    for w in watches:
        last = w.get("last_alert")
        if last and (now - datetime.fromisoformat(last)).total_seconds() \
                < WATCH_COOLDOWN_H * 3600:
            continue
        try:
            import asyncio as _aio
            # x_search calls asyncio.run() internally — from the scheduler's
            # event loop that raises. Run in a thread (the same loop-safety
            # fix the TG x_trends tool needed on 2026-08-24).
            posts = await _aio.to_thread(
                websearch.x_search, cfg, w["topic"], 10)
        except Exception as e:  # noqa: BLE001 — X flakiness never kills checks
            db.log("watch", f"search failed for '{w['topic']}': {e}",
                   level="warn")
            continue
        fresh = [p for p in posts if p.get("created_at") and
                 _hours_old(p["created_at"]) <= 24]
        if len(fresh) >= TREND_HIT_MIN:
            top = max(fresh, key=lambda p: p.get("likes") or 0)
            msg = (f"📈 Trend watch: '{w['topic']}' is active — "
                   f"{len(fresh)} fresh posts in 24h. Top: "
                   f"\"{(top.get('text') or '')[:100]}\" "
                   f"({top.get('likes', 0)} likes). Want a draft from it?")
            try:
                if tg.is_enabled():
                    tg.notify_bg(msg)
                    w["last_alert"] = now.isoformat(timespec="seconds")
                    alerted += 1
            except Exception:  # noqa: BLE001
                pass
    db.set_setting(WATCH_KEY, watches)
    if alerted:
        db.log("watch", f"{alerted} trend alert(s) sent "
                        f"({len(watches)} watches)")
    return {"checked": len(watches), "alerted": alerted}


def _hours_old(created) -> float:
    """Age in hours of a searched post. Live x_search returns twikit's
    X-format stamps — the ISO-only parser scored every real result 999h
    old, so trend watches could NEVER alert in production (dryrun and
    tests emit ISO, so the suite never saw it). Normalize with the same
    canonical converter the DB write path uses."""
    from ..core.db import _norm_created_at
    try:
        s = _norm_created_at(created)
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return (now - dt).total_seconds() / 3600
    except (TypeError, ValueError):
        return 999.0


def continue_thread(cfg: Config, draft_id: int = 0, addition: str = "") -> dict:
    """Reply to your own published thread — keep the conversation going
    while it's warm. Finds the thread's last tweet and drafts a reply
    under it."""
    from .llm import chat as llm_chat, LLMError
    from . import voice as voice_mod
    d = db.get_draft(draft_id)
    if not d or d["status"] != "published" or not d.get("x_id"):
        return {"ok": False, "error": f"#{draft_id} is not a published post"}
    add = addition.strip() or "a natural continuation of the conversation"
    try:
        raw = llm_chat(
            cfg.llm,
            system="You write ONE X reply continuing the owner's own "
                   "published thread — add value, never repeat. Output "
                   'STRICT JSON {"tweet": "..."}' +
                   voice_mod.voice_prompt_block(),
            user=f"ORIGINAL THREAD START:\n{d['text'][:600]}\n\n"
                 f"CONTINUATION ANGLE: {add}\n\nWrite the reply tweet now.",
            temperature=0.8, json_mode=True)
        from .llm import extract_json
        from ..core.text import scrub_ai_punctuation as scrub
        text = scrub(str(extract_json(raw).get("tweet") or ""))
    except LLMError as e:
        return {"ok": False, "error": f"continuation failed: {e}"}
    if not text or len(text) > 280:
        return {"ok": False, "error": "continuation empty or too long"}
    did = db.add_draft(
        text=text, kind="reply", temperature="bold",
        scheduled_at=(datetime.now() + timedelta(minutes=30)
                      ).isoformat(timespec="seconds"),
        meta={"source": "thread-continue", "reply_to_x_id": d["x_id"],
              "continues_draft": draft_id})
    return {"ok": True, "draft_id": did, "text": text[:200]}


def winner_quote_followup(cfg: Config, draft_id: int = 0,
                          winner_text: str = "") -> dict:
    """When a post crosses 2x baseline, draft a quote-post follow-up
    riding its momentum."""
    from .llm import chat as llm_chat, extract_json, LLMError
    from . import voice as voice_mod
    d = db.get_draft(draft_id)
    if not d or not d.get("x_id"):
        return {"ok": False, "error": f"#{draft_id} has no x_id"}
    try:
        raw = llm_chat(
            cfg.llm,
            system="You write ONE X quote-post riding the momentum of the "
                   "owner's viral post — add a NEW insight or expand the "
                   "take. Output STRICT JSON " +
                   '{"tweet": "..."}' + voice_mod.voice_prompt_block(),
            user=f"VIRAL ORIGINAL ({d['text'][:400]})\n\n"
                 f"Winner context: {winner_text[:200]}\n\n"
                 "Write the quote-post text now.",
            temperature=0.85, json_mode=True)
        from ..core.text import scrub_ai_punctuation as scrub
        text = scrub(str(extract_json(raw).get("tweet") or ""))
    except LLMError as e:
        return {"ok": False, "error": f"quote follow-up failed: {e}"}
    if not text:
        return {"ok": False, "error": "quote text empty"}
    did = db.add_draft(
        text=text, kind="quote", quote_of=d["x_id"], temperature="bold",
        meta={"source": "winner-quote", "rides_draft": draft_id})
    return {"ok": True, "draft_id": did, "text": text[:200]}
