"""Safety layer for cookie mode — keep the account looking human.

- Per-day caps on posts/replies
- Jittered human-like delays before every write
- Counters persisted in DB settings (reset by date)
"""
from __future__ import annotations

import asyncio
import random
from datetime import date

from . import db

TODAY = date.today().isoformat()


class SafetyCapExceeded(Exception):
    """Raised when a daily cap would be exceeded. Publish loop catches + reschedules."""


def _counters() -> dict:
    c = db.get_setting("safety_counters") or {}
    if c.get("date") != TODAY:
        c = {"date": TODAY, "posts": 0, "replies": 0}
    return c


def _save(c: dict) -> None:
    db.set_setting("safety_counters", c)


def usage() -> dict:
    c = _counters()
    return {"date": c["date"], "posts": c.get("posts", 0), "replies": c.get("replies", 0)}


def check_and_record(kind: str, caps: dict) -> None:
    """kind: 'posts' | 'replies'. Raises SafetyCapExceeded if over cap."""
    c = _counters()
    cap = caps.get(f"max_{kind}_per_day", 99)
    if c.get(kind, 0) >= cap:
        raise SafetyCapExceeded(
            f"Daily cap reached for {kind}: {c.get(kind)}/{cap}. Skipping until tomorrow."
        )
    c[kind] = c.get(kind, 0) + 1
    _save(c)


async def human_delay(delay_range: tuple[int, int] = (3, 15), joke: bool = False) -> None:
    """Sleep a random human-ish duration before a write action."""
    lo, hi = delay_range
    await asyncio.sleep(random.uniform(lo, hi))
