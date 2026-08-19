"""Mention inbox — never miss a reply to the agent (v0.3.9).

Someone @-mentioning the account is a direct conversation, not a search
result. The mentions loop pulls them, stores unseen ones in `seen_mentions`,
and drafts on-voice replies with the SAME approval gate as everything else.
Mention replies bypass the engage quality gate (that gate scores whether a
SEARCH target is worth a cold reply — someone talking to us directly is
always worth answering).

Publish stays human-gated: this module reads and drafts, nothing else.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..core import db
from . import brain as brain_mod
from . import voice_lock
from .algorithm import score_draft
from .lang import detect
from .llm import chat, extract_json, LLMError
from .style_scan import voice_match
from .voice import voice_prompt_block
from ..core.config import Config

MENTION_SYSTEM = """You are the account owner's reply agent on X, answering
someone who mentioned you directly.
{voice}

Rules:
- They talked to US — answer them like a human answering a human.
  Match the owner's voice EXACTLY. Human-casual, zero corporate.
- Add real value: answer the question, sharpen their point, or push back
  with a reason. Never just "thanks!".
- If they replied to one of OUR posts, address what THAT post said too.
- If the mention is Arabic, reply in Arabic (matching its dialect register).
- < 200 chars. No hashtags. No links.
- Return STRICT JSON: {{"reply": "..."}}"""

FETCH_LIMIT = 30            # mentions pulled per run
PENDING_CAP = 50            # hard cap on the pending query


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _handle(m: dict) -> str:
    return (m.get("author_handle") or m.get("author") or "").lstrip("@")


def _normalize(m: dict) -> Optional[dict]:
    """Raw client mention → the canonical inbox row shape."""
    x_id = str(m.get("x_id") or "").strip()
    author = _handle(m)
    if not x_id or not author:
        return None
    conversation_id = m.get("conversation_id") or x_id
    parent_id = m.get("in_reply_to_x_id")
    if not parent_id and conversation_id and conversation_id != x_id:
        parent_id = conversation_id
    return {
        "x_id": x_id,
        "author": author,
        "text": m.get("text") or "",
        "created_at": m.get("created_at"),
        "tweet_link": (f"https://x.com/{author}/status/{x_id}"
                       if x_id.isdigit() else m.get("tweet_link")),
        "conversation_id": conversation_id,
        "reply_to_me": int(bool(m.get("reply_to_me"))),
        # fetch-time enrichment (not persisted — the live dict carries it
        # from fetch_mentions() into draft_mention_reply())
        "_parent_x_id": parent_id,
        "parent_text": m.get("parent_text"),
    }


def _is_own(c, x_id: str, author: str, me: str, acct: int) -> bool:
    """Own tweets never enter the inbox (self-mention noise, echoed posts)."""
    if me and author.lower() == me.lower():
        return True
    row = c.execute("SELECT is_own FROM posts WHERE account_id=? AND x_id=?",
                    (acct, x_id)).fetchone()
    return bool(row and row["is_own"])


async def fetch_mentions(x, limit: int = FETCH_LIMIT,
                         acct: int | None = None) -> list[dict]:
    """Pull mentions → store unseen ones. Returns the NEW normalized rows.

    Operates on the ACTIVE account. Skips own tweets; dedupes by x_id
    against `posts` + `seen_mentions`. When the client says the mention
    replies to one of our posts (or carries a parent id), fetches the
    parent text as conversation context.
    """
    if acct is None:
        acct = db.active_account()
    me = db.get_me(acct).get("username") \
        or getattr(x, "username", "") or ""
    stored: list[dict] = []
    try:
        raw = await x.mentions(limit=limit)
    except Exception as e:  # noqa: BLE001 — a failed read must not break the loop
        db.log("mentions", f"fetch failed: {e}", level="error")
        return []
    for m in raw or []:
        n = _normalize(m)
        if n is None:
            continue
        x_id, author = n["x_id"], n["author"]
        parent_text = None
        reply_to_me = bool(n["reply_to_me"])
        parent_id = n.pop("_parent_x_id", None)
        if parent_id:
            try:  # conversation context — best-effort, never fatal
                p = await x.get_tweet(parent_id)
                parent_text = (p.get("text") or "") or None
                p_author = (p.get("author") or "").lstrip("@")
                reply_to_me = reply_to_me or (me and p_author.lower() == me.lower())
            except Exception:  # noqa: BLE001
                pass
        try:
            with db.connect() as c:
                if _is_own(c, x_id, author, me, acct):
                    continue
                before = c.total_changes
                c.execute(
                    "INSERT OR IGNORE INTO seen_mentions "
                    "(account_id, x_id, author, text, created_at, first_seen, handled) "
                    "VALUES (?,?,?,?,?,?,0)",
                    (acct, x_id, author, n["text"], n["created_at"], _now()),
                )
                if c.total_changes == before:
                    continue  # already seen
        except Exception as e:  # noqa: BLE001
            db.log("mentions", f"store {x_id} failed: {e}", level="error")
            continue
        n["reply_to_me"] = int(reply_to_me)
        if parent_text:
            n["parent_text"] = parent_text
        stored.append(n)
    db.log("mentions", f"fetched {len(raw or [])} mentions, {len(stored)} new")
    return stored


def _rows(where: str, limit: int, acct: Optional[int] = None) -> list[dict]:
    a = db.active_account() if acct is None else acct
    with db.connect() as c:
        rows = c.execute(
            f"SELECT * FROM seen_mentions WHERE account_id=? AND {where} "
            "ORDER BY created_at IS NULL, created_at DESC, first_seen DESC "
            "LIMIT ?", (a, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["x_id"].isdigit() and d["author"]:
            d["tweet_link"] = f"https://x.com/{d['author']}/status/{d['x_id']}"
        else:
            d["tweet_link"] = None
        d["conversation_id"] = d.get("conversation_id") or d["x_id"]
        d["reply_to_me"] = int(d.get("reply_to_me") or 0)
        out.append(d)
    return out


def pending_mentions(limit: int = PENDING_CAP,
                      acct: int | None = None) -> list[dict]:
    """Seen but not yet handled (handled = a draft reply exists). Newest first."""
    return _rows("handled=0", limit, acct=acct)


def recent_mentions(limit: int = PENDING_CAP) -> list[dict]:
    """The whole inbox, newest first — the non-pending API view."""
    return _rows("1=1", limit)


def mark_handled(x_id: str, acct: Optional[int] = None) -> None:
    a = db.active_account() if acct is None else acct
    with db.connect() as c:
        c.execute("UPDATE seen_mentions SET handled=1 WHERE account_id=? AND x_id=?",
                  (a, x_id))


def _mention_draft_statuses(acct: Optional[int] = None) -> dict[str, dict]:
    """x_id → latest mention-sourced reply draft {id, status} (for the API)."""
    a = db.active_account() if acct is None else acct
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, status, meta_json FROM drafts WHERE account_id=? AND kind='reply' "
            "ORDER BY id", (a,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except (ValueError, TypeError):
            continue
        if meta.get("source") == "mention" and meta.get("reply_to_x_id"):
            out[meta["reply_to_x_id"]] = {"id": r["id"], "status": r["status"]}
    return out


def mentions_view(pending_only: bool = True, limit: int = PENDING_CAP) -> list[dict]:
    """Normalized mentions + draft status — the /api/mentions payload
    (active-account scoped)."""
    rows = pending_mentions(limit) if pending_only else recent_mentions(limit)
    drafts = _mention_draft_statuses()
    out = []
    for r in rows:
        r.pop("parent_text", None)
        r["draft"] = drafts.get(r["x_id"])
        out.append(r)
    return out


def draft_mention_reply(cfg: Config, mention: dict,
                       acct: int | None = None) -> Optional[int]:
    """Draft an on-voice reply to one mention. Returns draft id, or None.

    Marks the mention handled ONLY when a draft actually exists. Approval
    gate applies as usual — this never publishes.
    """
    author = (mention.get("author") or "").lstrip("@")
    text = mention.get("text") or ""
    user = (f"@{author} mentioned us:\n{text}\n\n")
    parent = mention.get("parent_text")
    if mention.get("reply_to_me") and parent:
        user += (f"They are replying to OUR post:\n{parent}\n\n")
    user += "Write the reply."
    try:
        raw = chat(cfg.llm,
                   brain_mod.brain_context() + "\n\n" +
                   MENTION_SYSTEM.format(voice=voice_prompt_block()),
                   user, temperature=0.8, json_mode=True)
        reply = str(extract_json(raw).get("reply", ""))[:280]
    except LLMError as e:
        db.log("mentions", f"reply draft for {mention.get('x_id')} failed: {e}",
               level="error")
        return None
    if not reply:
        db.log("mentions", f"reply draft for {mention.get('x_id')} empty", level="warn")
        return None
    reply, vmeta = voice_lock.apply_voice_lock(cfg, reply, "reply")
    if reply is None:  # off-voice — mention stays pending for a retry
        return None
    alg = score_draft(reply, kind="reply")
    meta = {"reply_to_x_id": mention["x_id"],
            "target_author": author,
            "target_text": text[:200],
            "target_score": None,   # mention replies bypass the engage gate
            "source": "mention",
            "language": detect(reply),
            "alg": alg,
            "voice_match": voice_match(reply)}
    if vmeta:
        meta["voice"] = vmeta
    did = db.add_draft(text=reply, kind="reply", meta=meta, acct=acct)
    mark_handled(mention["x_id"], acct=acct)
    db.log("mentions", f"drafted reply to @{author} (draft {did}, approval-gated)")
    return did


def stats() -> dict[str, Any]:
    """Tiny counters for dashboards/tests (active-account scoped)."""
    a = db.active_account()
    with db.connect() as c:
        (pending,) = c.execute(
            "SELECT COUNT(*) FROM seen_mentions WHERE account_id=? AND handled=0",
            (a,)).fetchone()
        (total,) = c.execute(
            "SELECT COUNT(*) FROM seen_mentions WHERE account_id=?", (a,)).fetchone()
    return {"pending": pending, "total": total}
