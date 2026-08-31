"""Safety layer for cookie mode — keep the account looking human.

- Per-day caps on posts/replies
- Jittered human-like delays before every write
- Counters persisted in DB settings, keyed PER ACCOUNT (v0.5.0) and reset by date
"""
from __future__ import annotations

import asyncio
import random
from datetime import date

from . import db

TODAY = date.today().isoformat()


class SafetyCapExceeded(Exception):
    """Raised when a daily cap would be exceeded. Publish loop catches + reschedules."""


PREMIUM_MAX_CHARS = 25000  # X Premium single-post limit


def max_post_chars(acct: int | None = None) -> int:
    """The account's REAL single-post ceiling. X Premium accounts post up
    to 25,000 chars; free accounts 280 (owner 2026-08-31: @Mr_CryptoYT is
    Premium — the agent kept claiming '277/280 is the physical ceiling'
    while the owner asked for a long post four times)."""
    try:
        if db.get_acct_setting("x_premium", acct=acct):
            return PREMIUM_MAX_CHARS
    except Exception:  # noqa: BLE001 — capability lookup never breaks posting
        pass
    return 280


def _key(acct: int | None) -> str:
    a = db.active_account() if acct is None else acct
    return f"safety_counters:{a}"


def _counters(acct: int | None = None) -> dict:
    c = db.get_setting(_key(acct))
    if c is None and acct is None:  # pre-v0.5 single-install counters carry over
        legacy = db.get_setting("safety_counters")
        c = legacy if isinstance(legacy, dict) and legacy.get("date") == TODAY else None
    c = c or {}
    if c.get("date") != TODAY:
        c = {"date": TODAY, "posts": 0, "replies": 0}
    return c


def _save(c: dict, acct: int | None = None) -> None:
    db.set_setting(_key(acct), c)


def usage(acct: int | None = None) -> dict:
    c = _counters(acct)
    return {"date": c["date"], "posts": c.get("posts", 0), "replies": c.get("replies", 0)}


def check_and_record(kind: str, caps: dict, acct: int | None = None) -> None:
    """kind: 'posts' | 'replies'. Raises SafetyCapExceeded if over cap.

    acct: the account the write is for — counters are per account_id, so one
    account hitting its cap never blocks another (v0.5.0)."""
    c = _counters(acct)
    cap = caps.get(f"max_{kind}_per_day", 99)
    if c.get(kind, 0) >= cap:
        raise SafetyCapExceeded(
            f"Daily cap reached for {kind}: {c.get(kind)}/{cap}. Skipping until tomorrow."
        )
    c[kind] = c.get(kind, 0) + 1
    _save(c, acct)


async def human_delay(delay_range: tuple[int, int] = (3, 15), joke: bool = False) -> None:
    """Sleep a random human-ish duration before a write action."""
    lo, hi = delay_range
    await asyncio.sleep(random.uniform(lo, hi))
