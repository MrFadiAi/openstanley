"""Metrics ground truth — real engagement numbers, captured over time.

The account is LIVE: every learn tick pulls the owner's recent tweets (reads
only, existing client methods — never new raw calls) and appends what X
reports to two append-only time-series tables:

  metric_snapshots   — one row per post per refresh (likes/reposts/replies/
                       impressions). The earliest row is the post's baseline
                       and is never rewritten; history only grows.
  identity_snapshots — one row per refresh (followers) → follower growth.

posts.metrics_json always carries the LATEST capture; the posts row itself
never rewrites created_at, so "when was this published" stays first-seen
truth. Per-post engagement rate = (likes+reposts+replies)/max(followers,1) —
follower-normalized, the number a growth manager actually reads.

refresh_metrics ends with a hash-gated brain.reflect("metrics", …): the
brain learns what actually worked, but only when the summary changed
materially since the last metrics reflection — no journal spam per tick.

Aggregations here feed /api/analytics/{growth,top,times}; times_of_day
replaces the best-hours heuristic with real data once >=20 own posts exist.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from ..core import db

REFRESH_LIMIT = 60            # own tweets pulled per refresh (brief default)
HASH_SETTING = "metrics_reflect_hash"
REAL_TIMES_MIN_POSTS = 20     # below this, best-hours stays heuristic
TOP_HOURS = 3                 # how many hours get recommended as "best"
RATE_DECIMALS = 5
MAX_GROWTH_DAYS = 90


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- pure math (tested without any DB) ----------

def engagement_rate(likes: int, reposts: int, replies: int,
                    followers: int) -> float:
    """Follower-normalized engagement: (likes+reposts+replies)/max(followers,1)."""
    raw = (int(likes or 0) + int(reposts or 0) + int(replies or 0)) \
        / max(int(followers or 0), 1)
    return round(raw, RATE_DECIMALS)


def _rate_of(post: dict, followers: int) -> float:
    return engagement_rate(post.get("likes", 0), post.get("reposts", 0),
                           post.get("replies", 0), followers)


def _hour_of(created_at: Optional[str]) -> Optional[int]:
    if not created_at or "T" not in created_at:
        return None
    try:
        return int(created_at[11:13])
    except (ValueError, IndexError):
        return None


# ---------- snapshot writes (append-only) ----------

def append_metric_snapshot(post_x_id: str, captured_at: str, likes: int,
                           reposts: int, replies: int, impressions: int,
                           acct: int | None = None) -> None:
    with db._lock, db.connect() as c:
        c.execute(
            "INSERT INTO metric_snapshots (account_id, post_x_id, captured_at, "
            "likes, reposts, replies, impressions) VALUES (?,?,?,?,?,?,?)",
            (db._acct(acct), post_x_id, captured_at, int(likes or 0),
             int(reposts or 0), int(replies or 0), int(impressions or 0)),
        )


def append_identity_snapshot(captured_at: str, followers: int,
                              acct: int | None = None) -> None:
    with db._lock, db.connect() as c:
        c.execute(
            "INSERT INTO identity_snapshots (account_id, captured_at, followers) "
            "VALUES (?,?,?)",
            (db._acct(acct), captured_at, int(followers or 0)),
        )


def _set_latest_metrics(post: dict, captured_at: str, followers: int,
                         acct: int | None = None) -> None:
    """posts.metrics_json = the latest capture (time series keeps the rest)."""
    payload = {
        "captured_at": captured_at,
        "likes": int(post.get("likes", 0) or 0),
        "reposts": int(post.get("reposts", 0) or 0),
        "replies": int(post.get("replies", 0) or 0),
        "impressions": int(post.get("impressions", 0) or 0),
        "bookmarks": int(post.get("bookmarks", 0) or 0),
        "rate": _rate_of(post, followers),
    }
    with db._lock, db.connect() as c:
        c.execute("UPDATE posts SET metrics_json=? WHERE x_id=? AND account_id=?",
                  (json.dumps(payload), post.get("x_id"), db._acct(acct)))


def latest_followers(acct: int | None = None) -> int:
    """Most recent follower count (identity snapshot > stored identity > 0)."""
    with db.connect() as c:
        row = c.execute("SELECT followers FROM identity_snapshots "
                        "WHERE account_id=? "
                        "ORDER BY captured_at DESC, id DESC LIMIT 1",
                        (db._acct(acct),)).fetchone()
    if row:
        return int(row["followers"])
    me = db.get_me(acct)
    try:
        return int(me.get("followers") or 0)
    except (TypeError, ValueError):
        return 0


# ---------- refresh engine ----------

def _summarize(own: list[dict], followers: int, captured_at: str) -> dict:
    rates = [_rate_of(p, followers) for p in own]
    avg = round(sum(rates) / len(rates), RATE_DECIMALS) if rates else 0.0
    top = max(own, key=lambda p: _rate_of(p, followers)) if own else None
    return {
        "captured_at": captured_at,
        "posts": len(own),
        "followers": followers,
        "total_likes": sum(int(p.get("likes") or 0) for p in own),
        "total_reposts": sum(int(p.get("reposts") or 0) for p in own),
        "total_replies": sum(int(p.get("replies") or 0) for p in own),
        "avg_engagement_rate": avg,
        "top_post": {"x_id": top.get("x_id"), "rate": _rate_of(top, followers),
                     "text": (top.get("text") or "")[:140]} if top else None,
    }


def summary_hash(summary: dict) -> str:
    """Stable hash of the material facts only — timestamps excluded so an
    unchanged week never looks 'new'."""
    material = {k: summary.get(k) for k in
                ("posts", "followers", "total_likes", "total_reposts",
                 "total_replies", "avg_engagement_rate")}
    material["top_post"] = (summary.get("top_post") or {}).get("x_id")
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def render_summary(summary: dict) -> str:
    """Human/LLM-readable material for reflect('metrics')."""
    top = summary.get("top_post") or {}
    return (f"METRICS REFRESH — own posts {summary.get('posts')}, followers "
            f"{summary.get('followers')}, avg engagement rate "
            f"{summary.get('avg_engagement_rate')} (follower-normalized), "
            f"likes {summary.get('total_likes')}, reposts "
            f"{summary.get('total_reposts')}, replies "
            f"{summary.get('total_replies')}. Top post "
            f"[{top.get('rate', 0)} rate]: {top.get('text', '')[:110]}")


async def refresh_metrics(x, cfg, limit: int = REFRESH_LIMIT,
                         acct: int | None = None) -> dict:
    """Pull own recent tweets w/ metrics → posts upsert + time series +
    hash-gated brain reflection. Reads only; existing client methods only."""
    me = db.get_me(acct)
    try:
        fresh = await x.me()  # keeps followers current for identity series
        me = {**me, **fresh}
        db.set_me(me, acct)
    except Exception as e:  # noqa: BLE001 — stored identity is a fine fallback
        db.log("metrics", f"me() refresh failed, using stored identity: {e}",
               level="warn")
    followers = int(me.get("followers") or 0)
    captured_at = _now()
    if followers:
        append_identity_snapshot(captured_at, followers, acct)

    username = me.get("username") or getattr(x, "username", "") or ""
    own: list[dict] = []
    if username:
        own = await x.user_tweets(username, limit=limit)
    for p in own:
        p["is_own"] = 1
        db.upsert_post(p, acct)  # conflict path updates metrics, keeps created_at
        append_metric_snapshot(p.get("x_id"), captured_at,
                               p.get("likes", 0), p.get("reposts", 0),
                               p.get("replies", 0), p.get("impressions", 0),
                               acct)
        _set_latest_metrics(p, captured_at, followers, acct)

    summary = _summarize(own, followers, captured_at)
    reflected = await _maybe_reflect(cfg, summary, acct)
    db.log("metrics", f"[account {db._acct(acct)}] refresh: {len(own)} posts "
                      f"captured, followers={followers}, "
                      f"avg_rate={summary['avg_engagement_rate']}"
                      f", reflect={'yes' if reflected else 'skipped'}")
    return {"refreshed": len(own), "reflected": reflected, **summary}


async def _maybe_reflect(cfg, summary: dict, acct: int | None = None) -> bool:
    """brain.reflect('metrics') only when the summary changed materially."""
    h = summary_hash(summary)
    key = HASH_SETTING if db._acct(acct) == 1 else f"{HASH_SETTING}:{db._acct(acct)}"
    if h == db.get_setting(key):
        return False
    from . import brain  # lazy: brain never imports metrics at module load
    try:
        await asyncio.to_thread(
            brain.reflect, cfg, "metrics",
            {"material": render_summary(summary),
             "note": "metrics refresh — real performance data"}, acct)
        db.set_setting(key, h)  # only on success: LLM-down retries next tick
        return True
    except Exception as e:  # noqa: BLE001 — reflection must never break refresh
        db.log("metrics", f"reflect(metrics) failed: {e}", level="warn")
        return False


# ---------- aggregations (growth / top / times) ----------

def growth_series(days: int = 14) -> dict:
    """Daily aggregates over the window: followers (forward-filled), posts
    published, avg engagement rate, best post of the day."""
    days = max(1, min(int(days or 14), MAX_GROWTH_DAYS))
    today = date.today()
    since = (today - timedelta(days=days - 1)).isoformat()
    a = db.active_account()
    with db.connect() as c:
        frows = c.execute(
            "SELECT captured_at, followers FROM identity_snapshots "
            "WHERE account_id=? AND captured_at >= ? "
            "ORDER BY captured_at, id", (a, since)).fetchall()
        carry = c.execute(
            "SELECT followers FROM identity_snapshots "
            "WHERE account_id=? AND captured_at < ? "
            "ORDER BY captured_at DESC, id DESC LIMIT 1", (a, since)).fetchone()
        prows = c.execute(
            "SELECT x_id, author_handle, text, created_at, likes, reposts, replies "
            "FROM posts WHERE account_id=? AND is_own=1 AND created_at >= ? "
            "ORDER BY created_at", (a, since)).fetchall()
    followers_by_day: dict[str, int] = {}
    for r in frows:
        followers_by_day[r["captured_at"][:10]] = int(r["followers"])
    followers = int(carry["followers"]) if carry else None

    posts_by_day: dict[str, list[dict]] = defaultdict(list)
    for r in prows:
        posts_by_day[(r["created_at"] or "")[:10]].append(dict(r))
    rate_followers = latest_followers()

    series: list[dict] = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        if d in followers_by_day:
            followers = followers_by_day[d]
        day_posts = posts_by_day.get(d, [])
        rates = [_rate_of(p, rate_followers) for p in day_posts]
        best = max(day_posts, key=lambda p: _rate_of(p, rate_followers)) \
            if day_posts else None
        series.append({
            "date": d,
            "followers": followers,
            "posts": len(day_posts),
            "avg_engagement_rate": round(sum(rates) / len(rates), RATE_DECIMALS)
                                    if rates else None,
            "best_post": _public_post(best, rate_followers, rank=None)
                         if best else None,
        })
    f_values = [s["followers"] for s in series if s["followers"] is not None]
    start, end = (f_values[0], f_values[-1]) if f_values else (None, None)
    return {"days": days, "series": series,
            "followers_start": start, "followers_end": end,
            "followers_delta": (end - start) if start is not None and end is not None else None,
            "total_posts": len(prows)}


def _public_post(p: dict, followers: int, rank: Optional[int]) -> dict:
    handle = p.get("author_handle") or (db.get_me()).get("username", "")
    x_id = p.get("x_id")
    return {
        "rank": rank,
        "x_id": x_id,
        "text": (p.get("text") or "")[:140],
        "created_at": p.get("created_at"),
        "likes": int(p.get("likes") or 0),
        "reposts": int(p.get("reposts") or 0),
        "replies": int(p.get("replies") or 0),
        "impressions": int(p.get("impressions") or 0),
        "rate": _rate_of(p, followers),
        "url": f"https://x.com/{handle}/status/{x_id}" if x_id and handle else None,
    }


def top_posts(limit: int = 10, days: int = 30) -> list[dict]:
    """Top own posts by follower-normalized engagement rate in the window."""
    limit = max(1, min(int(limit or 10), 50))
    days = max(1, min(int(days or 30), 365))
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as c:
        rows = c.execute(
            "SELECT x_id, author_handle, text, created_at, impressions, likes, "
            "reposts, replies FROM posts WHERE account_id=? AND is_own=1 "
            "AND created_at >= ?",
            (db.active_account(), since)).fetchall()
    followers = latest_followers()
    scored = sorted((dict(r) for r in rows),
                    key=lambda p: (_rate_of(p, followers), p.get("created_at") or ""),
                    reverse=True)[:limit]
    return [_public_post(p, followers, rank=i + 1) for i, p in enumerate(scored)]


def times_of_day(days: int = 60) -> dict:
    """Engagement by hour-of-day from OWN posts. `source` is 'real' once
    >= REAL_TIMES_MIN_POSTS posts exist in the window, else 'heuristic'."""
    days = max(1, min(int(days or 60), 365))
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as c:
        rows = c.execute(
            "SELECT created_at, likes, reposts, replies FROM posts "
            "WHERE account_id=? AND is_own=1 AND created_at >= ?",
            (db.active_account(), since)).fetchall()
    by_hour: dict[int, dict] = {h: {"posts": 0, "engagement": 0}
                                for h in range(24)}
    for r in rows:
        h = _hour_of(r["created_at"])
        if h is None or h not in by_hour:
            continue
        slot = by_hour[h]
        slot["posts"] += 1
        slot["engagement"] += int(r["likes"] or 0) + int(r["reposts"] or 0) \
            + int(r["replies"] or 0)
    hours = [{"hour": h, "posts": s["posts"],
              "engagement": s["engagement"],
              "avg_engagement": round(s["engagement"] / s["posts"], 3)
                                if s["posts"] else 0.0}
             for h, s in by_hour.items()]
    total = sum(s["posts"] for s in by_hour.values())
    if total >= REAL_TIMES_MIN_POSTS:
        source = "real"
        ranked = sorted((h for h in hours if h["posts"] >= 2),
                        key=lambda h: h["avg_engagement"], reverse=True) \
            or sorted(hours, key=lambda h: h["engagement"], reverse=True)
        best = sorted(h["hour"] for h in ranked[:TOP_HOURS])
    else:
        from .algorithm import DEFAULT_BEST_HOURS  # lazy: no import cycle
        source = "heuristic"
        best = sorted(DEFAULT_BEST_HOURS)
    return {"source": source, "total_posts": total,
            "min_posts_for_real": REAL_TIMES_MIN_POSTS,
            "best_hours": best, "hours": hours}


def best_hours_for_scoring() -> Optional[set[int]]:
    """Real best hours for algorithm.score_draft, or None when data is thin
    (< REAL_TIMES_MIN_POSTS posts) so callers keep their heuristic."""
    data = times_of_day()
    if data["source"] != "real":
        return None
    return set(data["best_hours"])
