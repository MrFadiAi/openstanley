"""Insights v2 — the Stanley-style analytics surface, computed from real data.

One module, one job: turn the posts table (own posts = the connected
account's history) into the aggregates the Insights page renders:
account summary, year heatmap, 30-day growth, interaction orbit,
milestones, content-type performance and the best-content-to-repost wall.

Everything degrades gracefully on a young/quiet account — zeros and empty
lists, never fabricated numbers.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

from ..core import db


def _d(ts: str) -> Optional[date]:
    """Parse the posts table's assorted timestamp shapes → date."""
    if not ts:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(ts).strip()[:26], fmt).date()
        except ValueError:
            continue
    try:  # last resort: ISO prefix
        return date.fromisoformat(str(ts)[:10])
    except ValueError:
        return None


def _own_posts(acct: Optional[int] = None) -> list[sqlite3.Row]:
    with db.connect() as c:
        return c.execute(
            "SELECT p.x_id, p.text, p.created_at, p.impressions, p.likes, "
            "p.reposts, p.replies, p.engagement, d.image AS draft_image "
            "FROM posts p LEFT JOIN "
            "(SELECT x_id, MAX(image) AS image FROM drafts "
            " WHERE image IS NOT NULL GROUP BY x_id) d ON d.x_id = p.x_id "
            "WHERE p.account_id=? AND p.is_own=1 ORDER BY p.created_at",
            (db._acct(acct),)).fetchall()




def _eng(p) -> float:
    """ABSOLUTE engagement for a post row (posts.engagement stores a RATE
    — insights divided percentages by impressions and every ranking went
    garbage: the 'best post' wall led with a 1-like reply, live 2026-09-02
    'still no real data')."""
    return ((p["likes"] or 0) + 3 * (p["reposts"] or 0)
            + 8 * (p["replies"] or 0))


def account_summary(acct: Optional[int] = None) -> dict:
    me = db.get_me() or {}
    account = db.get_account(db.active_account()) or {}
    today = date.today()
    today_imp = 0
    for p in _own_posts(acct):
        if _d(p["created_at"]) == today:
            today_imp += p["impressions"] or 0
    return {"handle": me.get("username") or account.get("handle") or "",
            "followers": me.get("followers") or account.get("followers"),
            "today_impressions": today_imp}


def heatmap(acct: Optional[int] = None, days: int = 364) -> list[dict]:
    """Daily own-post impressions for the trailing year — GitHub-style."""
    by_day: dict[str, int] = {}
    for p in _own_posts(acct):
        d = _d(p["created_at"])
        if d:
            key = d.isoformat()
            by_day[key] = by_day.get(key, 0) + (p["impressions"] or 0)
    start = date.today() - timedelta(days=days)
    out = []
    cur = start
    while cur <= date.today():
        key = cur.isoformat()
        out.append({"date": key, "value": by_day.get(key, 0)})
        cur += timedelta(days=1)
    return out


def growth(acct: Optional[int] = None, days: int = 30) -> dict:
    """Impressions in the last N days vs the N before, plus the series."""
    by_day: dict[str, int] = {}
    for p in _own_posts(acct):
        d = _d(p["created_at"])
        if d:
            by_day[d.isoformat()] = by_day.get(d.isoformat(), 0) + (p["impressions"] or 0)
    today = date.today()
    cur_total = sum(by_day.get((today - timedelta(days=i)).isoformat(), 0)
                    for i in range(days))
    prev_total = sum(by_day.get((today - timedelta(days=i)).isoformat(), 0)
                     for i in range(days, days * 2))
    if prev_total > 0:
        delta = round(100.0 * (cur_total - prev_total) / prev_total, 1)
    else:
        delta = None  # no history to compare against — UI shows the raw total
    series = [{"date": (today - timedelta(days=i)).isoformat(),
               "impressions": by_day.get((today - timedelta(days=i)).isoformat(), 0)}
              for i in range(days - 1, -1, -1)]
    return {"days": days, "total": cur_total, "prev_total": prev_total,
            "delta_pct": delta, "series": series}


def orbit(acct: Optional[int] = None, limit: int = 12) -> list[dict]:
    """Accounts you actually talked with (reply targets, mention authors).
    Bubble size = interactions on record."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT author_handle, COUNT(*) AS n FROM engagements "
            "WHERE account_id=? AND author_handle IS NOT NULL "
            "AND author_handle != '' GROUP BY author_handle "
            "ORDER BY n DESC LIMIT ?",
            (db._acct(acct), limit)).fetchall()
    return [{"handle": r["author_handle"].lstrip("@"), "interactions": r["n"]}
            for r in rows]


_MILESTONES = [
    # (id, label, current_fn, target)
    ("followers_100", "100 followers", lambda s: s["followers"] or 0, 100),
    ("followers_1k", "1,000 followers", lambda s: s["followers"] or 0, 1000),
    ("followers_10k", "10,000 followers", lambda s: s["followers"] or 0, 10000),
    ("followers_100k", "100,000 followers", lambda s: s["followers"] or 0, 100000),
    ("imp_1k", "1,000 impressions", lambda s: s["total_impressions"], 1000),
    ("imp_100k", "100k impressions", lambda s: s["total_impressions"], 100000),
    ("imp_1m", "1M impressions", lambda s: s["total_impressions"], 1000000),
    ("posts_10", "10 posts", lambda s: s["post_count"], 10),
    ("posts_100", "100 posts", lambda s: s["post_count"], 100),
    ("likes_100", "100 likes", lambda s: s["total_likes"], 100),
]


def milestones(acct: Optional[int] = None) -> list[dict]:
    posts = _own_posts(acct)
    s = {"followers": (db.get_me() or {}).get("followers") or 0,
         "total_impressions": sum(p["impressions"] or 0 for p in posts),
         "post_count": len(posts),
         "total_likes": sum(p["likes"] or 0 for p in posts)}
    return [{"id": mid, "label": label, "current": fn(s),
             "target": target, "unlocked": fn(s) >= target}
            for mid, label, fn, target in _MILESTONES]


def type_performance(acct: Optional[int] = None) -> dict:
    """Text vs image posts. Image = published via a draft that carried one
    (posts.x_id ↔ drafts.image) — real plumbing that fills as media ships."""
    posts = _own_posts(acct)

    def _bucket(rows):
        n = len(rows)
        imp = sum(p["impressions"] or 0 for p in rows)
        eng = sum(_eng(p) for p in rows)
        return {"count": n,
                "avg_impressions": round(imp / n) if n else 0,
                "engagement_rate": round(eng / imp, 4) if imp else 0.0}

    text_rows = [p for p in posts if not p["draft_image"]]
    image_rows = [p for p in posts if p["draft_image"]]
    ratio = None
    if image_rows and text_rows:
        ta = sum(p["impressions"] or 0 for p in text_rows) / len(text_rows)
        ia = sum(p["impressions"] or 0 for p in image_rows) / len(image_rows)
        if ia > 0:
            ratio = round(ta / ia, 1) if ta >= ia else round(ia / ta, 1)
    return {"text": _bucket(text_rows), "image": _bucket(image_rows),
            "leader": ("text" if not image_rows else
                       "text" if (text_rows and (not image_rows or
                       sum(p["impressions"] or 0 for p in text_rows) / len(text_rows) >=
                       sum(p["impressions"] or 0 for p in image_rows) / len(image_rows))) else "image"),
            "ratio": ratio}


def best_content(acct: Optional[int] = None, limit: int = 12) -> list[dict]:
    """Top own posts by engagement rate — the repost wall."""
    posts = []
    for p in _own_posts(acct):
        imp = p["impressions"] or 0
        eng = _eng(p)
        posts.append({
            "x_id": p["x_id"], "text": p["text"] or "",
            "created_at": str(p["created_at"] or "")[:10],
            "likes": p["likes"] or 0, "impressions": imp,
            "engagement": eng,
            "engagement_rate": round(eng / imp, 4) if imp else 0.0,
            "image": p["draft_image"],
        })
    posts.sort(key=lambda p: (-p["engagement"], -p["impressions"]))
    return posts[:limit]


def overview(acct: Optional[int] = None) -> dict:
    return {"account": account_summary(acct),
            "heatmap": heatmap(acct),
            "growth": growth(acct),
            "orbit": orbit(acct),
            "milestones": milestones(acct),
            "types": type_performance(acct),
            "best": best_content(acct)}
