"""Weekly owner report — Friday evening, one message that makes the whole
system legible: what shipped, what it earned, what the brain learned and
forgot, what awaits the owner's decision.

Owner request 2026-09-02 ('lets do 6'): the dashboard shows fragments;
this is the narrative — the week in one honest message.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..core import db
from ..core.config import Config

ROOT = Path(__file__).resolve().parent.parent.parent

RULES_ARCHIVE = ROOT / "data" / "accounts"


def _abs_eng(p) -> float:
    return ((p["likes"] or 0) + 3 * (p["reposts"] or 0)
            + 8 * (p["replies"] or 0))


def build_weekly(cfg: Config, acct: Optional[int] = None) -> str:
    """Assemble the weekly report from real DB state."""
    a = db._acct(acct)
    now = datetime.now()
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    # 1. what shipped
    with db.connect() as c:
        shipped = c.execute(
            "SELECT id, text, x_id, published_at FROM drafts "
            "WHERE account_id=? AND status='published' AND published_at >= ? "
            "ORDER BY published_at", (a, week_ago)).fetchall()
        approved_waiting = c.execute(
            "SELECT id, text, scheduled_at FROM drafts "
            "WHERE account_id=? AND status='approved' ORDER BY scheduled_at "
            "LIMIT 6", (a,)).fetchall()
        rejected_n = c.execute(
            "SELECT COUNT(*) FROM drafts WHERE account_id=? "
            "AND status='rejected' AND created_at >= ?", (a, week_ago)
        ).fetchone()[0]
        # fresh-reject owner rejections this week (learning signal)
        # engagement deltas on this week's shipped posts
        week_posts = c.execute(
            "SELECT text, likes, reposts, replies, impressions FROM posts "
            "WHERE account_id=? AND is_own=1 AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 60", (a, week_ago)).fetchall()

    lines = ["📋 **Weekly Report** — "
             f"{(now - timedelta(days=7)).strftime('%b %d')} → "
             f"{now.strftime('%b %d %Y')}", ""]

    # 2. what it earned
    total_eng = sum(_abs_eng(p) for p in week_posts)
    total_imp = sum(p["impressions"] or 0 for p in week_posts)
    me = db.get_me(a) or {}
    lines += ["**Shipped this week:**",
              f"• {len(shipped)} post(s) published, "
              f"{len(approved_waiting)} approved waiting in queue, "
              f"{rejected_n} rejected"]
    if week_posts:
        top = max(week_posts, key=_abs_eng)
        lines += [f"• Week's engagement: {total_eng:,.0f} on "
                  f"{total_imp:,} impressions",
                  f"• Best: \"{(top['text'] or '')[:60]}\" "
                  f"({top['likes']}♥)"]
    lines.append(f"• Followers: {me.get('followers', '?'):,}"
                 if isinstance(me.get("followers"), int) else
                 f"• Followers: {me.get('followers', '?')}")

    # 3. what the brain learned
    rules_now = db.get_acct_setting("brain_rules_snapshot", acct=a)
    try:
        from . import brain as brain_mod
        strat = brain_mod.read("strategies", a)
        theses = [ln.strip("- ").strip()
                  for ln in strat.split("## Working theses")[1]
                  .split("## Experiment log")[0].splitlines()
                  if ln.strip().startswith("- ")][:4]
        experiments = [ln for ln in
                       strat.split("## Experiment log")[1].splitlines()
                       if ln.strip().startswith("- ")][-4:]
    except Exception:  # noqa: BLE001 — brain read is best-effort
        theses, experiments = [], []
    lines += ["", "**What the brain learned:**"]
    if theses:
        lines += [f"• {t[:90]}" for t in theses]
    else:
        lines.append("• (no theses yet this week)")
    if experiments:
        lines += ["", "**Latest experiments:**"]
        lines += [f"• {e[2:100].strip()}" for e in experiments]

    # 4. what awaits the owner
    if approved_waiting:
        lines += ["", "**Waiting on your approval:**"]
        lines += [f"• #{r['id']} — {(r['text'] or '')[:50]}"
                  for r in approved_waiting]

    lines.append("")
    lines.append(f"— OpenStanley · full detail on the dashboard")
    return "\n".join(lines)


def push_weekly(cfg: Config, acct: Optional[int] = None) -> bool:
    """Friday evening: deliver the report to the owner's Telegram."""
    from ..integrations import telegram as tg
    if not tg.is_enabled():
        db.log("weekly", "weekly report skipped — TG not enabled")
        return False
    text = build_weekly(cfg, acct)
    tg.notify(text)
    db.log("weekly", "weekly owner report pushed")
    return True
