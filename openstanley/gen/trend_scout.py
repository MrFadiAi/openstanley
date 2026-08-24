"""Trend scout — daily live X research in the account's niche.

Runs ahead of the create loop: pulls fresh posts for each niche theme/
account through the cookie session, picks genuinely FRESH topics (not
covered by our own recent posts/drafts), and drafts from the real
findings. The draft rides the same gates as everything else: voice,
diversity, dash-scrub, hook-card, approval.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from ..core import db
from ..core.config import Config

MIN_HOURS_FRESH = 36          # only scout posts newer than this
MAX_SCOUT_DRAFTS = 1          # per run — trend drafts complement the bank, not replace it
SIM_BAR = 0.5                 # vs our own recent content


def _fresh_on_x(p: dict, now: datetime) -> bool:
    created = str(p.get("created_at") or "")
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(created.strip()[:31], fmt)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)   # compare against naive now()
            age_h = (now - dt).total_seconds() / 3600
            return 0 <= age_h <= MIN_HOURS_FRESH
        except ValueError:
            continue
    return False  # undatable → not provably fresh → skipped


async def scout(cfg: Config, x, acct: Optional[int] = None) -> dict:
    """Pull niche conversations from the last 36h → fresh, non-duplicate
    topics. Read-only; caps untouched. Returns findings for drafting."""
    now = datetime.now()
    themes = list(cfg.agent.evergreen_themes or [])[:3]
    found: list[dict] = []
    for theme in themes:
        try:
            posts = await x.search(theme, limit=15)
        except Exception as e:  # noqa: BLE001 — one theme failing is fine
            db.log("trend-scout", f"search '{theme}' failed: {e}", level="warn")
            continue
        for p in posts:
            if not _fresh_on_x(p, now):
                continue
            found.append({"theme": theme,
                          "text": (p.get("text") or "")[:280],
                          "author": p.get("author_handle") or "?",
                          "likes": p.get("likes") or 0})
    found.sort(key=lambda f: -f["likes"])
    db.log("trend-scout", f"{len(found)} fresh niche posts "
                          f"(last {MIN_HOURS_FRESH}h) across {len(themes)} themes")
    return {"found": found}


def draft_from_findings(cfg: Config, findings: list[dict],
                        acct: Optional[int] = None) -> Optional[int]:
    """The freshest finding that ISN'T a rerun of our own content → one
    on-voice draft grounded in it. Approval-gated like every draft."""
    from .llm import chat, extract_json
    from . import voice as voice_mod
    from . import diversity as div
    from ..core.text import scrub_ai_punctuation

    if not findings:
        return None
    own = div.recent_draft_texts(acct)  # dedup vs our recent drafts
    pick = None
    for f in findings:
        if not any(div.similarity(f["text"], prev) >= SIM_BAR for prev in own):
            pick = f
            break
    if pick is None:
        db.log("trend-scout", "all fresh topics overlap recent drafts — skipping")
        return None

    material = chr(10).join(
        f"@{f['author']}: {f['text']}" for f in findings[:6])
    fmt = ("story", "open with the concrete detail from the finding, then "
           "your take as the agent running this account")
    vb = div.variety_block(own, fmt, div.question_budget(own))
    voice = voice_mod.voice_prompt_block()  # ACTIVE account voice+style
    system = (
        "You write ONE X post in the user's voice from LIVE niche findings. "
        'Output STRICT JSON: {"tweet": "..."}. Under 240 chars. Ground it in '
        "a specific detail from the findings. No hashtags, no question mark "
        "at the end.")
    user = (f"LIVE NICHE FINDINGS (last hours):" + chr(10) + material[:2200]
            + chr(10) + f"CHOSEN TOPIC: @{pick['author']}: {pick['text']}"
            + chr(10) + f"USER VOICE: {str(voice)[:350]}" + vb
            + chr(10) + "Write the post now.")
    raw = chat(cfg.llm, system, user, json_mode=True)
    data = extract_json(raw)
    text = scrub_ai_punctuation(
        (data.get("tweet") or "").strip()) if isinstance(data, dict) else ""
    if not text or div.too_similar(text, own):
        db.log("trend-scout", "trend draft rejected (empty or too similar)")
        return None
    image = None
    try:
        from . import quote_card
        image = quote_card.make_card(text)
    except Exception:  # noqa: BLE001
        image = None
    did = db.add_draft(text=text, kind="post", temperature="bold",
                       image=image, acct=db._acct(acct),
                       meta={"source": "trend-scout",
                             "topic": pick["text"][:120],
                             "topic_author": pick["author"]})
    db.log("trend-scout", f"draft #{did} from live topic by @{pick['author']}")
    return did
