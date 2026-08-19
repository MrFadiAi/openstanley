"""Reply drafting — OpenStanley's "outreach and comments 24/7" (approval-gated).

v0.3: in addition to mentions, the engage loop finds niche-relevant target
posts and drafts replies that are SCHEDULED, not sent — same approval flow
as posts.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from ..core import db
from . import brain as brain_mod
from . import engage_gate
from . import voice_lock
from .algorithm import score_draft
from .lang import detect
from .llm import chat, extract_json, LLMError
from .style_scan import voice_match
from .voice import voice_prompt_block
from ..core.config import Config

REPLY_SYSTEM = """You are the account owner's reply agent on X.
{voice}

Rules:
- Replies must feel human-casual, not corporate. Match the owner's voice EXACTLY.
- Add value: answer, sharpen, or playfully challenge. Never just "Great post!".
- < 200 chars. No hashtags. Return STRICT JSON: {{"reply": "..."}}"""

NICHE_REPLY_SYSTEM = """You are the account owner engaging with their niche on X.
{voice}

You are writing a REPLY to another account's post — visibility play: replies
on big niche posts put this voice in front of the right audience.

Rules:
- Match the owner's voice EXACTLY. Human-casual, zero corporate.
- Add real value: a sharper angle, a number, a gentle counter, a useful addition.
  Never "Great post!". Never start with "I".
- Reply to the CONTENT, referencing specifics from the post.
- If the original is Arabic, reply in Arabic (matching its dialect register).
- < 180 chars. No hashtags. No links.
- Return STRICT JSON: {{"reply": "..."}}"""


async def pull_engagements(cfg: Config, x_client) -> int:
    """Fetch mentions/notifications → engagements table (ACTIVE account).
    Returns count of new."""
    acct = db.active_account()
    mentions = await x_client.mentions(limit=30)
    new = 0
    for m in mentions:
        try:
            with db.connect() as c:
                before = c.total_changes
                c.execute(
                    "INSERT OR IGNORE INTO engagements (account_id, x_id, kind, author_handle, author_name, text, status, created_at, seen_at) "
                    "VALUES (?,?,?,?,?,?,'new',?,?)",
                    (acct, m.get("x_id"), m.get("kind", "mention"), m.get("author_handle", ""),
                     m.get("author_name", ""), m.get("text", ""),
                     m.get("created_at"), db._now()),
                )
                if c.total_changes > before:
                    new += 1
        except Exception as e:  # noqa: BLE001
            db.log("engage", f"insert mention failed: {e}", level="error")
    db.log("engage", f"pulled mentions: {len(mentions)} total, {new} new")
    return new


def draft_replies(cfg: Config, limit: int = 8) -> list[int]:
    """Draft on-voice replies for new engagements (ACTIVE account)."""
    acct = db.active_account()
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM engagements WHERE account_id=? AND status='new' "
            "ORDER BY created_at DESC LIMIT ?",
            (acct, limit),
        ).fetchall()
    if not rows:
        return []
    ids = []
    for e in rows:
        e = dict(e)
        try:
            raw = chat(cfg.llm,
                       brain_mod.brain_context() + "\n\n" +
                       REPLY_SYSTEM.format(voice=voice_prompt_block()),
                       f"Original post that mentioned us:\n@{e['author_handle']}: {e['text']}\n\nWrite the reply.",
                       temperature=0.8, json_mode=True)
            reply = str(extract_json(raw).get("reply", ""))[:280]
            if not reply:
                continue
            reply, vmeta = voice_lock.apply_voice_lock(cfg, reply, "reply")
            if reply is None:  # off-voice — engagement stays 'new' for a retry
                continue
            alg = score_draft(reply, kind="reply")
            meta = {"engagement_id": e["id"],
                    "reply_to_x_id": e["x_id"],
                    "author": e["author_handle"],
                    "language": detect(reply),
                    "alg": alg,
                    "voice_match": voice_match(reply)}
            if vmeta:
                meta["voice"] = vmeta
            did = db.add_draft(text=reply, kind="reply", meta=meta)
            with db.connect() as c:
                c.execute("UPDATE engagements SET status='drafted', draft_id=? WHERE id=?",
                          (did, e["id"]))
            ids.append(did)
        except LLMError as err:
            db.log("engage", f"reply draft failed: {err}", level="error")
    db.log("engage", f"drafted {len(ids)} replies")
    return ids


# ---------- scheduled niche replies (v0.3) ----------

def _existing_reply_targets() -> set[str]:
    """x_ids we already have a reply draft for (any status — never double-reply)."""
    acct = db.active_account()
    with db.connect() as c:
        rows = c.execute(
            "SELECT meta_json FROM drafts WHERE account_id=? AND kind='reply'",
            (acct,)).fetchall()
    out = set()
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "{}")
            if meta.get("reply_to_x_id"):
                out.add(meta["reply_to_x_id"])
        except (ValueError, TypeError):
            pass
    return out


def _pick_niche_targets(cfg: Config, limit: int = 5) -> list[dict]:
    """High-engagement niche posts matched by account relevance."""
    already = _existing_reply_targets()
    niche_accounts = set(cfg.agent.niche_accounts or [])
    profile = db.get_acct_setting("style_profile") or {}
    topics = set((profile.get("stats") or {}).get("topics") or [])
    scored = []
    for p in db.niche_posts(limit=80):
        if p.get("x_id") in already or not p.get("text"):
            continue
        relevance = 0.0
        if p.get("author_handle") in niche_accounts:
            relevance += 2.0
        low = (p["text"] or "").lower()
        relevance += sum(1 for t in topics if t.lower() in low) * 0.5
        relevance += min(2.0, (p.get("engagement") or 0) / 10)
        scored.append((relevance, p))
    scored.sort(key=lambda x: -x[0])
    return [p for rel, p in scored[:limit] if rel > 0]


def draft_niche_replies(cfg: Config, limit: int = 3) -> list[int]:
    """Draft SCHEDULED replies to niche targets → approval flow, never auto-send.

    v0.3.8: targets pass the engage quality gate FIRST — dead or crowded
    tweets are rejected before any LLM call, so cap budget is only spent
    on targets worth a reply.
    """
    candidates = _pick_niche_targets(cfg, limit=limit * 3)
    kept, _rejected = engage_gate.filter_targets(cfg, candidates, datetime.now())
    targets = kept[:limit]
    if not targets:
        db.log("engage", "no niche targets worth a reply right now "
                         f"(gate kept 0 of {len(candidates)})")
        return []
    ids = []
    for i, (target, tscore) in enumerate(targets):
        # stagger within the next 1-3 hours so replies look organic
        when = (datetime.now() + timedelta(minutes=45 + 55 * i + random.randint(0, 20)))
        try:
            raw = chat(cfg.llm,
                       brain_mod.brain_context() + "\n\n" +
                       NICHE_REPLY_SYSTEM.format(voice=voice_prompt_block()),
                       f"TARGET POST by @{target['author_handle']} "
                       f"({target.get('likes', 0)} likes):\n{target['text']}\n\n"
                       f"Write the reply.",
                       temperature=0.85, json_mode=True)
            reply = str(extract_json(raw).get("reply", ""))[:280]
        except LLMError as e:
            db.log("engage", f"niche reply draft failed: {e}", level="error")
            continue
        if not reply:
            continue
        reply, vmeta = voice_lock.apply_voice_lock(cfg, reply, "reply")
        if reply is None:  # off-voice — target stays in the pool for a retry
            continue
        alg = score_draft(reply, kind="reply")
        meta = {"reply_to_x_id": target["x_id"],
                "target_author": target["author_handle"],
                "target_text": (target.get("text") or "")[:200],
                "target_score": tscore.meta(),
                "source": "engage-niche",
                "language": detect(reply),
                "alg": alg,
                "voice_match": voice_match(reply)}
        if vmeta:
            meta["voice"] = vmeta
        did = db.add_draft(
            text=reply, kind="reply",
            scheduled_at=when.isoformat(timespec="seconds"),
            meta=meta)
        ids.append(did)
    if ids:
        db.log("engage", f"drafted {len(ids)} SCHEDULED niche replies (approval-gated)")
    return ids


def mark_replied(engagement_id: int) -> None:
    with db.connect() as c:
        c.execute("UPDATE engagements SET status='replied' WHERE id=? AND account_id=?",
                  (engagement_id, db.active_account()))
