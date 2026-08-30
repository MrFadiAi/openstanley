"""Morning briefing — the agent messages the OWNER first (09:00 daily).

Stanley's real magic: you approve from bed. Instead of the owner asking
"what's scheduled?", the briefing lands on their phone with everything
that matters and one-tap buttons on what needs a decision.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from ..core import db
from ..core.config import Config


def build_briefing(cfg: Config, acct: Optional[int] = None) -> dict:
    """Assemble the morning briefing: overnight publishes (with best
    performer), drafts needing attention, today's schedule, caps."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    out: dict = {"date": today}

    with db.connect() as c:
        a = db._acct(acct)
        # overnight publishes (yesterday evening -> now)
        published = c.execute(
            "SELECT id, text, x_id FROM drafts WHERE account_id=? "
            "AND status='published' AND published_at >= ? ORDER BY id DESC",
            (a, yesterday + "T17:00")).fetchall()
        out["published_overnight"] = [
            {"id": r["id"], "text": (r["text"] or "")[:100]} for r in published]
        # drafts needing attention: pending + fresh (last 12h)
        pending = c.execute(
            "SELECT id, text, kind, meta_json FROM drafts WHERE account_id=? "
            "AND status='draft' AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 6",
            (a, (datetime.now() - timedelta(hours=12)
                 ).isoformat(timespec="seconds"))).fetchall()
        import json as _json
        needs = []
        for r in pending:
            meta = _json.loads(r["meta_json"] or "{}")
            alg = meta.get("alg") or {}
            needs.append({"id": r["id"], "text": (r["text"] or "")[:100],
                          "kind": r["kind"], "alg_score": alg.get("score")})
        out["needs_attention"] = needs
        # today's approved schedule
        sched = c.execute(
            "SELECT id, text, scheduled_at FROM drafts WHERE account_id=? "
            "AND status='approved' AND scheduled_at LIKE ? "
            "ORDER BY scheduled_at", (a, today + "%")).fetchall()
        out["today_schedule"] = [
            {"id": r["id"], "when": (r["scheduled_at"] or "")[11:16],
             "text": (r["text"] or "")[:80]} for r in sched]
        # rejection stats last 24h
        (rej,) = c.execute(
            "SELECT COUNT(*) FROM drafts WHERE account_id=? "
            "AND status='rejected' AND created_at >= ?",
            (a, (datetime.now() - timedelta(hours=24)
                 ).isoformat(timespec="seconds"))).fetchone()
        out["rejected_24h"] = rej
    return out


def render_briefing(cfg: Config, acct: Optional[int] = None) -> str:
    """The TG-ready briefing text."""
    b = build_briefing(cfg, acct)
    lines = [f"☀️ Morning briefing — {b['date']}", ""]
    pub = b["published_overnight"]
    if pub:
        lines.append(f"**Shipped overnight: {len(pub)} post(s)**")
        for p in pub[:3]:
            lines.append(f"• #{p['id']}: {p['text']}")
    else:
        lines.append("**Nothing shipped overnight**")
    lines.append("")
    needs = b["needs_attention"]
    if needs:
        lines.append(f"**Needs your eye ({len(needs)} fresh drafts):**")
        for n in needs[:4]:
            score = f" · alg {n['alg_score']}" if n.get("alg_score") else ""
            lines.append(f"• #{n['id']} [{n['kind']}{score}] {n['text']}")
    today = b["today_schedule"]
    if today:
        lines.append("")
        lines.append(f"**Today's schedule ({len(today)}):**")
        for t in today[:5]:
            lines.append(f"• {t['when']} #{t['id']}: {t['text']}")
    if b["rejected_24h"]:
        lines.append("")
        lines.append(f"_(You rejected {b['rejected_24h']} in the last 24h "
                     "— I've been learning from them)_")
    lines.append("")
    lines.append("Tap approve/reject on the cards above, or ask me anything.")
    return "\n".join(lines)


def push_briefing(cfg: Config, acct: Optional[int] = None) -> bool:
    """Send the briefing + draft cards to the owner's Telegram."""
    from ..integrations import telegram as tg
    if not tg.is_enabled():
        return False
    text = render_briefing(cfg, acct)
    tg.notify(text)
    needs = build_briefing(cfg, acct)["needs_attention"]
    if needs:
        tg.notify_new_drafts([n["id"] for n in needs[:4]])
    db.log("briefing", "morning briefing pushed")
    return True
