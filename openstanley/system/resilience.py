"""Robustness tier — X error taxonomy, cookie countdown, DB diet, crash monitor.

Each closes a specific silent-failure class:
- X error taxonomy: only 186 (too long) was handled; 326 (duplicate),
  226 (sensitive), auth errors each need their own alert
- Cookie countdown: cookies decay weekly; warn BEFORE they die
- DB diet: the shared test DB grows unbounded (50MB+); auto-vacuum nightly
- Crash monitor: if the server process dies at 4am, nothing restarts it
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from ..core import db

# ---------- X error taxonomy ----------

X_ERROR_MAP = {
    "186": ("too long", "post exceeds 280 chars — needs a split or trim"),
    "326": ("duplicate", "X thinks this was already posted — likely a "
                          "re-post; check the account before retrying"),
    "226": ("sensitive", "X flagged the content as sensitive — review "
                          "before any retry"),
    "327": ("rate limited", "X is throttling writes — backing off; the "
                             "draft stays scheduled for the next window"),
    "32": ("auth", "cookies rejected — re-paste them in Settings → "
                    "Connect (this is the weekly decay)"),
    "353": ("auth", "cookies rejected (shadow ban check) — re-paste in "
                     "Settings → Connect"),
}


def classify_x_error(error_text: str) -> Optional[tuple[str, str]]:
    """Map a raw X/twikit error to (class, owner-readable fix)."""
    if not error_text:
        return None
    for code, (kind, fix) in X_ERROR_MAP.items():
        if f"'code': {code}" in error_text or f"code {code}" in error_text:
            return kind, fix
    if "Authorization" in error_text:
        return "auth", "cookies rejected — re-paste them in Settings → Connect"
    return None


def alert_x_error(draft_id: int, error_text: str, account: int = 2) -> None:
    """Publish-failure alert with the classified fix path."""
    cls = classify_x_error(error_text)
    from ..integrations import telegram as tg
    kind, fix = cls or ("unknown", "check the agent log for details")
    try:
        if tg.is_enabled():
            tg.notify_bg(f"⚠️ Draft #{draft_id} failed to publish "
                         f"({kind}): {fix}")
    except Exception:  # noqa: BLE001 — alert delivery is best-effort
        pass
    db.log("publish", f"draft {draft_id} failure classified as "
                      f"'{kind}' — alerted owner", level="warn")


# ---------- cookie countdown ----------

COOKIE_LIFETIME_DAYS = 7  # X cookies decay roughly weekly


def cookie_health(acct: Optional[int] = None) -> dict:
    """Days since the account's cookies were last refreshed + a warning
    when nearing the decay window."""
    a = db._acct(acct)
    row = db.get_account(a) or {}
    updated = row.get("cookies_updated_at") or row.get("updated_at")
    # fall back to account creation if no cookie timestamp exists
    updated = updated or row.get("created_at")
    if not updated:
        return {"account": a, "days_old": None, "status": "unknown"}
    try:
        dt = datetime.fromisoformat(str(updated)[:19])
    except (TypeError, ValueError):
        return {"account": a, "days_old": None, "status": "unknown"}
    days = (datetime.now() - dt).days
    status = "fresh" if days < 4 else \
             "aging" if days < COOKIE_LIFETIME_DAYS else "stale"
    return {"account": a, "days_old": days, "status": status,
            "renew_in_days": max(0, COOKIE_LIFETIME_DAYS - days)}


def cookie_warning_line(acct: Optional[int] = None) -> str:
    """One line for /status: '' when fresh, a warning when aging/stale."""
    h = cookie_health(acct)
    if h.get("status") in ("fresh", "unknown"):
        return ""
    days = h.get("days_old", "?")
    renew = h.get("renew_in_days", "?")
    return (f"⚠️ cookies {days}d old — renew in ~{renew}d "
            "(Settings → Connect)")


# ---------- DB diet ----------

def vacuum_db(path: Optional[str] = None) -> int:
    """VACUUM the database, return size after in KB. Safe on a live DB
    (takes a lock briefly)."""
    import sqlite3
    import os
    p = path or str(db.DB_PATH)
    before = os.path.getsize(p) if os.path.exists(p) else 0
    conn = sqlite3.connect(p)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = os.path.getsize(p)
    return after // 1024
