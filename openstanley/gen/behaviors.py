"""Autonomous behaviors — optional capabilities the create loop can use.

Owner ask 2026-09-13: 'make them as options when I activate them it will
do it by autonomous mode' — the two proven shapes (first-reply links,
landmark quote drafts) plus a GitHub drafting beat, each toggled from the
Loops page. OFF by default: the autonomous loop changes ONLY when the
owner flips a switch.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from ..core import db
from ..core.config import Config

PREBUILT = [
    {"id": "link_first_reply", "name": "Links as first reply",
     "description": "When an idea's source carries a URL, the link "
                    "ships as the FIRST REPLY under the post (clean "
                    "body) instead of inline text — same as your chat "
                    "requests. Shows as the teal bar on every draft."},
    {"id": "quote_landmarks", "name": "Quote landmark announcements",
     "description": "Top outlier ideas (the landmark-announcement "
                    "pattern — your 28x baseline winner) become QUOTE "
                    "drafts of the source tweet instead of plain posts. "
                    "The quoted tweet is clickable on every card."},
    {"id": "github_daily", "name": "Draft from your GitHub",
     "description": "Once a day, draft one post from your latest pushed "
                    "repos — grounded in the repo description and newest "
                    "commits (your own real work, never trending noise)."},
]

BEHAVIOR_SETTING = "autonomous_behaviors"   # {id: bool}


def get_behaviors() -> list[dict]:
    state = db.get_setting(BEHAVIOR_SETTING) or {}
    out = []
    for b in PREBUILT:
        row = dict(b)
        row["enabled"] = bool(state.get(b["id"], False))
        out.append(row)
    return out


def set_behavior(behavior_id: str, enabled: bool) -> bool:
    if behavior_id not in {b["id"] for b in PREBUILT}:
        return False
    state = db.get_setting(BEHAVIOR_SETTING) or {}
    state[behavior_id] = bool(enabled)
    db.set_setting(BEHAVIOR_SETTING, state)
    db.log("behaviors", f"{behavior_id} {'ENABLED' if enabled else 'disabled'} "
                        f"by owner — autonomous create loop updated")
    return True


def enabled(behavior_id: str) -> bool:
    state = db.get_setting(BEHAVIOR_SETTING) or {}
    return bool(state.get(behavior_id, False))


# ---------- applied inside drafts.generate_drafts ----------

def shape_draft(draft: dict, idea: dict) -> dict:
    """Post-process one fresh autonomous draft through the ENABLED
    behaviors. Returns the (possibly modified) draft dict."""
    src_xid = idea.get("source_x_id") or ""
    src_handle = idea.get("source_handle") or ""
    src_link = (f"https://x.com/{src_handle}/status/{src_xid}"
                if src_xid and src_handle else "")

    # 1. link_first_reply: source URL → first reply, out of the body
    if enabled("link_first_reply") and src_link:
        text = draft.get("text") or ""
        if src_link in text:
            text = text.replace(src_link, "").strip()
            draft["text"] = text
        draft.setdefault("meta", {})
        draft["meta"]["link_reply"] = src_link
        draft["behavior_link"] = True

    # 2. quote_landmarks: high-score outlier sources become quotes of
    # the source tweet (plain posts otherwise)
    if (enabled("quote_landmarks") and src_xid
            and float(idea.get("score") or 0) >= 8.0
            and draft.get("kind", "post") == "post"
            and not draft.get("thread")):
        draft["kind"] = "quote"
        draft["quote_of"] = src_xid
        draft["behavior_quote"] = True

    return draft


def github_beat_due(cfg: Config, acct: Optional[int] = None) -> bool:
    """github_daily runs at most once per calendar day."""
    if not enabled("github_daily"):
        return False
    key = _acct_key("behaviors:github_last", acct)
    return db.get_acct_setting(key) != date.today().isoformat()


def mark_github_done(acct: Optional[int] = None) -> None:
    db.set_acct_setting(_acct_key("behaviors:github_last", acct),
                         date.today().isoformat())


def _acct_key(key: str, acct: Optional[int]) -> str:
    if acct is None:
        return key
    return f"{key}:{acct}"
