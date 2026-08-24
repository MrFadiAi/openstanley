"""Steal-this-hook — reusable hook patterns mined from niche winners.

The study loop already stores the niche's posts; the top performers encode
what actually opens threads. One LLM pass distills them into reusable
PATTERNS (structure + why it works + the example), stored per-account;
`remix` writes a brand-new post in YOUR voice from a pattern — the same
approval gate as every other draft.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from ..core import db
from ..core.config import Config

SETTING_KEY = "hook_patterns"
MAX_HOOKS = 12


def _hooks(acct: Optional[int] = None) -> list[dict]:
    raw = db.get_setting(SETTING_KEY)
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []


def _save(hooks: list[dict], acct: Optional[int] = None) -> None:
    db.set_setting(SETTING_KEY, hooks[:MAX_HOOKS])


def _niche_winners(acct: Optional[int] = None, limit: int = 12) -> list[dict]:
    """Top niche posts by engagement rate — the raw material for patterns."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT text, author_handle, likes, reposts, engagement, impressions "
            "FROM posts WHERE account_id=? AND is_own=0 "
            "ORDER BY (CASE WHEN impressions > 50 THEN 1.0*engagement/impressions "
            "ELSE 0 END) DESC LIMIT ?",
            (db._acct(acct), limit)).fetchall()
    return [dict(r) for r in rows]


_EXTRACT_SYSTEM = (
    "You distill viral hook PATTERNS from top posts. Output STRICT JSON "
    'shaped {"hooks": [{"pattern": "...", "why": "...", "example": "..."}]} '
    "where pattern is the reusable structure, why is one line on the "
    "mechanic, example is the best concrete instance from the material. "
    "5-8 patterns, no duplicates in structure, plain English."
)


def extract(cfg: Config, acct: Optional[int] = None) -> dict:
    """Mine patterns from the stored niche winners. Safe to re-run: new
    patterns are deduped by token overlap against stored ones."""
    from .llm import chat, extract_json
    winners = _niche_winners(acct)
    if not winners:
        return {"added": 0, "total": len(_hooks(acct)), "reason": "no niche posts stored yet"}
    material = "\n\n".join(
        f"@{w['author_handle']}: {w['text'][:280]}" for w in winners)
    raw = chat(cfg.llm, _EXTRACT_SYSTEM, material, json_mode=True)
    data = extract_json(raw)
    fresh = data.get("hooks") if isinstance(data, dict) else data
    if not isinstance(fresh, list):
        fresh = []
    existing = _hooks(acct)
    existing_tokens = [{t.lower() for t in h.get("pattern", "").split()} for h in existing]

    added = 0
    for h in fresh:
        if not isinstance(h, dict) or not (h.get("pattern") or "").strip():
            continue
        toks = {t.lower() for t in h["pattern"].split()}
        if any(len(toks & e) / max(len(toks | e), 1) >= 0.5 for e in existing_tokens):
            continue  # same shape already banked
        existing.append({
            "id": max((x.get("id", 0) for x in existing), default=0) + 1 + added,
            "pattern": h["pattern"].strip()[:160],
            "why": (h.get("why") or "").strip()[:160],
            "example": (h.get("example") or "").strip()[:200],
            "added_at": datetime.now().isoformat(timespec="seconds"),
        })
        added += 1
    _save(existing, acct)
    db.log("hooks", f"extract: +{added} patterns (bank {len(existing)}) from "
                    f"{len(winners)} niche winners")
    return {"added": added, "total": len(existing)}


def list_hooks(acct: Optional[int] = None) -> list[dict]:
    return _hooks(acct)


_REMIX_SYSTEM = (
    "You write ONE X post in the user's voice using a given HOOK PATTERN. "
    "Output STRICT JSON: {\"text\": \"...\", \"kind\": \"post\"}. Keep it "
    "under 240 chars, concrete, no hashtags."
)


def remix(cfg: Config, hook_id: int, acct: Optional[int] = None) -> int | None:
    """Pattern → a fresh draft in the user's voice. Returns draft id."""
    from .llm import chat, extract_json
    from . import voice as voice_mod
    hook = next((h for h in _hooks(acct) if h.get("id") == hook_id), None)
    if not hook:
        return None
    voice = voice_mod.voice_prompt_block()  # ACTIVE account voice+style
    user = (f"HOOK PATTERN: {hook['pattern']}\n"
            f"WHY IT WORKS: {hook.get('why', '')}\n"
            f"EXAMPLE FROM THE NICHE: {hook.get('example', '')}\n"
            f"USER VOICE: {str(voice)[:400]}\n\nWrite the post now.")
    raw = chat(cfg.llm, _REMIX_SYSTEM, user, json_mode=True)
    data = extract_json(raw)
    text = (data.get("text") or "").strip() if isinstance(data, dict) else ""
    if not text:
        return None
    did = db.add_draft(
        text=text, kind="post", temperature="bold",
        meta={"source": "hook-remix", "hook_pattern": hook["pattern"]},
        acct=db._acct(acct))
    db.log("hooks", f"remix of pattern #{hook_id} → draft #{did}")
    return did
