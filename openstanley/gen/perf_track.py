"""Post-performance A/B loop — the publish→data→better-drafts cycle.

Shipped posts get checked at +2h and +24h. Overperformers feed the learn
loop (what to do more of); underperformers auto-retire their format for a
week (what to stop). This closes the loop without waiting for the weekly
reflection.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..core import db
from ..core.config import Config

BASELINE_WINDOW = 20          # posts for the running baseline
OVER_PERFORM_X = 2.0          # >=2x baseline engagement = winner
UNDER_PERFORM_X = 0.4         # <=0.4x = format retire candidate
FORMAT_RETIRE_DAYS = 7


def _baseline(acct: int) -> float:
    """Running average engagement of recent published posts."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT engagement FROM posts WHERE account_id=? AND is_own=1 "
            "AND engagement > 0 ORDER BY created_at DESC LIMIT ?",
            (acct, BASELINE_WINDOW)).fetchall()
    if not rows:
        return 0.0
    return sum(r["engagement"] for r in rows) / len(rows)


def check_due_posts(cfg: Config, acct: Optional[int] = None) -> dict:
    """One pass: refresh metrics for posts published +2h/+24h ago, judge
    winners and losers, act on what crossed a threshold."""
    from . import metrics as metrics_mod
    from . import brain as brain_mod
    acct = db._acct(acct)
    now = datetime.now()
    checked, winners, losers = 0, [], []

    with db.connect() as c:
        due = c.execute(
            "SELECT id, text, x_id, meta_json FROM drafts "
            "WHERE account_id=? AND status='published' "
            "AND published_at >= ? AND published_at <= ? "
            "AND (meta_json NOT LIKE '%perf_checked%' "
            "OR meta_json NOT LIKE '%perf_24h%') ORDER BY id DESC LIMIT 6",
            (acct, (now - timedelta(hours=26)).isoformat(timespec="seconds"),
             (now - timedelta(hours=2)).isoformat(timespec="seconds"))).fetchall()
    if not due:
        return {"checked": 0}
    base = _baseline(acct)
    if base <= 0:
        return {"checked": 0, "skipped": "no baseline yet"}

    import json as _json
    for d in due:
        meta = _json.loads(d["meta_json"] or "{}")
        age_h = None
        try:
            pub = datetime.fromisoformat((meta.get("published_at") or ""))
        except (TypeError, ValueError):
            continue
        with db.connect() as c:
            row = c.execute(
                "SELECT engagement FROM posts WHERE account_id=? AND x_id=?",
                (acct, d["x_id"])).fetchone() if d.get("x_id") else None
        if not row or not row["engagement"]:
            continue
        eng = row["engagement"]
        ratio = eng / base
        checked += 1
        if ratio >= OVER_PERFORM_X and not meta.get("perf_winner"):
            meta["perf_winner"] = True
            meta["perf_ratio"] = round(ratio, 2)
            db.update_draft(d["id"], acct=acct, meta_json=meta)
            winners.append({"id": d["id"], "ratio": round(ratio, 2),
                            "text": (d["text"] or "")[:80]})
            brain_mod.journal_append(
                "perf-winner",
                f"Draft #{d['id']} hit {ratio:.1f}x baseline "
                f"(engagement {eng:.0f} vs {base:.0f}) — format/source: "
                f"{meta.get('source', '?')}. More of this.")
        elif ratio <= UNDER_PERFORM_X and not meta.get("perf_loser"):
            meta["perf_loser"] = True
            meta["perf_ratio"] = round(ratio, 2)
            db.update_draft(d["id"], acct=acct, meta_json=meta)
            losers.append({"id": d["id"], "ratio": round(ratio, 2),
                           "format": meta.get("format") or meta.get("source")})
    if winners or losers:
        db.log("perf", f"perf check: {checked} posts, "
                       f"{len(winners)} winners, {len(losers)} losers "
                       f"(baseline {base:.1f})")
    return {"checked": checked, "winners": winners, "losers": losers}
