"""Metrics ground truth v0.3.6 — refresh engine, time series, aggregations,
hash-gated brain reflection, and the growth analytics API endpoints.

Hermetic: every test runs against a per-test throwaway SQLite (db.DB_PATH is
swapped to tmp_path) and deterministic fake X clients — no network, no LLM
(brain.reflect is replaced by a recorder).
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"  # before importing the server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openstanley.core import db  # noqa: E402

import openstanley.server.__main__ as server  # noqa: E402  (also init_db on import)

client = TestClient(server.app)

from openstanley.core.config import Config  # noqa: E402
from openstanley.gen import agent as agent_mod  # noqa: E402
from openstanley.gen import brain  # noqa: E402
from openstanley.gen import metrics  # noqa: E402
from openstanley.gen.metrics import engagement_rate  # noqa: E402

HANDLE = "metuser"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Each test gets its own empty DB — aggregation counts are exact."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "metrics.db")
    db.init_db()
    yield


@pytest.fixture
def fake_reflect(monkeypatch):
    """Record brain.reflect calls instead of hitting an LLM."""
    calls: list[dict] = []

    def _rec(cfg, trigger, payload=None):
        calls.append({"trigger": trigger, "payload": payload})
        return {"ok": True, "trigger": trigger, "applied": {}, "journal_entry": ""}

    monkeypatch.setattr(brain, "reflect", _rec)
    return calls


class FixedClient:
    """Deterministic X stand-in: same tweets + followers on every call."""

    def __init__(self, tweets: list[dict], followers: int = 100,
                 username: str = HANDLE):
        self._tweets = tweets
        self.followers = followers
        self.username = username

    async def me(self) -> dict:
        return {"username": self.username, "name": "Met User",
                "followers": self.followers}

    async def user_tweets(self, username: str, limit: int = 100) -> list[dict]:
        return copy.deepcopy(self._tweets[:limit])


def _tweet(x_id: str, *, likes=0, reposts=0, replies=0, impressions=0,
           created_at=None, text=None, handle=HANDLE) -> dict:
    return {"x_id": x_id, "author_handle": handle, "is_own": 1,
            "created_at": created_at
            or datetime.now().isoformat(timespec="seconds"),
            "text": text or f"post {x_id}", "impressions": impressions,
            "likes": likes, "reposts": reposts, "replies": replies,
            "bookmarks": 0}


def _run(coro):
    return asyncio.run(coro)


def _seed_identity(days_ago_followers: list[tuple[int, int]]) -> None:
    """(days_ago, followers) → identity_snapshots rows."""
    for days_ago, followers in days_ago_followers:
        captured = (datetime.now() - timedelta(days=days_ago)).isoformat(
            timespec="seconds")
        metrics.append_identity_snapshot(captured, followers)


# ---------------- pure math ----------------

def test_engagement_rate_math():
    assert engagement_rate(10, 2, 3, 100) == 0.15
    # zero followers must not divide by zero — denominator floors at 1
    assert engagement_rate(10, 0, 0, 0) == 10.0
    # rounding to RATE_DECIMALS
    assert engagement_rate(1, 1, 1, 7) == round(3 / 7, 5)
    # None-ish counts tolerated (upsert-path dicts carry None sometimes)
    assert engagement_rate(None, None, None, 50) == 0.0


# ---------------- refresh engine ----------------

def test_refresh_appends_time_series_and_keeps_earliest(fake_reflect):
    old_created = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    v1 = [_tweet("met-1", likes=5, reposts=1, replies=2, impressions=400,
                 created_at=old_created, text="first capture")]
    res1 = _run(metrics.refresh_metrics(FixedClient(v1, followers=100), Config()))
    assert res1["refreshed"] == 1 and res1["followers"] == 100
    assert res1["reflected"] is True and len(fake_reflect) == 1
    assert fake_reflect[0]["trigger"] == "metrics"
    assert "avg engagement rate" in fake_reflect[0]["payload"]["material"]

    # later capture: same post, higher numbers — history only grows
    v2 = [_tweet("met-1", likes=50, reposts=10, replies=20, impressions=4000,
                 created_at=old_created, text="first capture")]
    _run(metrics.refresh_metrics(FixedClient(v2, followers=100), Config()))

    with db.connect() as c:
        snaps = c.execute("SELECT * FROM metric_snapshots ORDER BY id").fetchall()
        ids = c.execute("SELECT COUNT(*) AS n FROM identity_snapshots").fetchone()
        row = c.execute("SELECT * FROM posts WHERE x_id='met-1'").fetchone()
    assert len(snaps) == 2, "one snapshot row per refresh (append-only)"
    assert [s["likes"] for s in snaps] == [5, 50], "earliest snapshot kept + latest"
    assert ids["n"] == 2, "identity (followers) snapshot per refresh"
    assert row["created_at"] == old_created, "created_at stays FIRST-seen"
    assert row["likes"] == 50, "posts row carries the LATEST metrics"
    import json as _json
    latest = _json.loads(row["metrics_json"])
    assert latest["likes"] == 50 and latest["rate"] == engagement_rate(50, 10, 20, 100)


def test_reflect_hash_gate_dedupes_identical_summaries(fake_reflect):
    tweets = [_tweet("met-gate", likes=8, reposts=2, replies=2, impressions=300)]
    x = FixedClient(tweets, followers=100)
    r1 = _run(metrics.refresh_metrics(x, Config()))
    r2 = _run(metrics.refresh_metrics(x, Config()))  # identical material
    assert r1["reflected"] is True and r2["reflected"] is False
    assert len(fake_reflect) == 1, "no journal spam on unchanged metrics"

    x._tweets = [_tweet("met-gate", likes=40, reposts=5, replies=9, impressions=900)]
    r3 = _run(metrics.refresh_metrics(x, Config()))
    assert r3["reflected"] is True and len(fake_reflect) == 2
    # the stored hash matches the latest summary
    assert db.get_setting(metrics.HASH_SETTING) == \
        metrics.summary_hash(metrics._summarize(x._tweets, 100, "any"))


def test_reflect_failure_leaves_gate_open(monkeypatch):
    """LLM down → hash NOT stored → the next refresh retries reflection."""
    def _boom(cfg, trigger, payload=None):
        raise RuntimeError("llm unreachable")

    def _ok(cfg, trigger, payload=None):
        return {"ok": True}

    monkeypatch.setattr(brain, "reflect", _boom)
    tweets = [_tweet("met-down", likes=3, replies=1)]
    r1 = _run(metrics.refresh_metrics(FixedClient(tweets), Config()))
    assert r1["reflected"] is False
    assert db.get_setting(metrics.HASH_SETTING) is None, "gate stays open on failure"

    monkeypatch.setattr(brain, "reflect", _ok)  # LLM back up
    r2 = _run(metrics.refresh_metrics(FixedClient(tweets), Config()))
    assert r2["reflected"] is True, "retry happened once the LLM came back"


def test_refresh_without_username_still_safe(fake_reflect, monkeypatch):
    """No stored identity and me() failing → refresh degrades, never raises."""
    def _me_fail(self):
        raise RuntimeError("offline")
    monkeypatch.setattr(FixedClient, "me", _me_fail)
    tweets = [_tweet("met-offline", likes=1)]
    res = _run(metrics.refresh_metrics(FixedClient(tweets), Config()))
    assert res["refreshed"] == 1 and res["followers"] == 0
    assert res["reflected"] is True  # material (zeros) still learned once


# ---------------- aggregations ----------------

def test_growth_series_over_fixture_days(fake_reflect):
    # followers: d-3=100, d-1=121 (d-2 missing → must carry 100 forward)
    _seed_identity([(3, 100), (1, 121)])
    d = lambda days, hh=12: (datetime.now() - timedelta(days=days)).replace(
        hour=hh, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    # two posts on d-2: 15 and 5 engagements; one post on d-3
    db.upsert_post(_tweet("met-g1", likes=15, reposts=0, replies=0,
                          created_at=d(2, 9)))
    db.upsert_post(_tweet("met-g2", likes=4, reposts=1, replies=0,
                          created_at=d(2, 18)))
    db.upsert_post(_tweet("met-g3", likes=2, created_at=d(3, 7)))

    g = metrics.growth_series(4)
    assert g["days"] == 4 and len(g["series"]) == 4
    by_date = {s["date"]: s for s in g["series"]}
    dates = sorted(by_date)  # [d-3, d-2, d-1, today]
    assert by_date[dates[0]]["followers"] == 100   # snapshot day
    assert by_date[dates[1]]["followers"] == 100   # carry-forward across the gap
    assert by_date[dates[2]]["followers"] == 121   # snapshot day
    assert by_date[dates[3]]["followers"] == 121   # today carries d-1's snapshot
    assert by_date[dates[1]]["posts"] == 2
    exp_avg = round((engagement_rate(15, 0, 0, 121)
                     + engagement_rate(4, 1, 0, 121)) / 2, 5)
    assert by_date[dates[1]]["avg_engagement_rate"] == exp_avg
    assert by_date[dates[1]]["best_post"]["x_id"] == "met-g1"
    assert by_date[dates[0]]["posts"] == 1
    assert by_date[dates[0]]["best_post"]["x_id"] == "met-g3"
    assert by_date[dates[2]]["posts"] == 0
    assert by_date[dates[2]]["avg_engagement_rate"] is None
    assert g["followers_delta"] == 21 and g["total_posts"] == 3
    # rate denominator is the LATEST follower count (121)
    assert by_date[dates[1]]["best_post"]["rate"] == engagement_rate(15, 0, 0, 121)


def test_top_posts_ordering_limit_and_fields():
    now = datetime.now().isoformat(timespec="seconds")
    for i, likes in enumerate((30, 3, 12, 25, 8)):
        db.upsert_post(_tweet(f"met-top{i}", likes=likes, reposts=0, replies=0,
                              created_at=now))
    db.set_setting("me", {"username": HANDLE, "followers": 100})
    top = metrics.top_posts(limit=3, days=30)
    assert len(top) == 3
    assert [p["rank"] for p in top] == [1, 2, 3]
    assert [p["x_id"] for p in top] == ["met-top0", "met-top3", "met-top2"]
    rates = [p["rate"] for p in top]
    assert rates == sorted(rates, reverse=True), "ordered by rate desc"
    assert top[0]["rate"] == 0.30
    assert top[0]["url"] == f"https://x.com/{HANDLE}/status/met-top0"
    assert top[0]["text"].startswith("post met-top0")
    # own posts only
    db.upsert_post({**_tweet("met-niche", likes=99), "is_own": 0,
                    "author_handle": "someone_else"})
    assert all(p["x_id"] != "met-niche" for p in metrics.top_posts(10, 30))


def test_times_of_day_real_vs_heuristic():
    def at(hour: int, days_ago: int = 1) -> str:
        return (datetime.now() - timedelta(days=days_ago)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        ).isoformat(timespec="seconds")

    # thin data → heuristic source, DEFAULT best hours
    db.upsert_post(_tweet("met-h1", likes=5, replies=2, created_at=at(10)))
    thin = metrics.times_of_day()
    assert thin["source"] == "heuristic"
    assert thin["total_posts"] < thin["min_posts_for_real"]
    assert thin["best_hours"] == sorted({8, 9, 12, 13, 17, 18, 21})
    assert metrics.best_hours_for_scoring() is None

    # 24 more posts: hour 9 strongly outperforms hour 21
    for i in range(12):
        db.upsert_post(_tweet(f"met-9-{i}", likes=40, reposts=4, replies=4,
                              created_at=at(9)))
        db.upsert_post(_tweet(f"met-21-{i}", likes=1, created_at=at(21)))
    real = metrics.times_of_day()
    assert real["source"] == "real" and real["total_posts"] == 25
    assert real["best_hours"] == [9, 21], "both qualify (>=2 posts), 9 leads by avg"
    by_hour = {h["hour"]: h for h in real["hours"]}
    assert by_hour[9]["posts"] == 12 and by_hour[9]["avg_engagement"] == 48.0
    assert by_hour[21]["posts"] == 12 and by_hour[21]["avg_engagement"] == 1.0
    assert metrics.best_hours_for_scoring() == {9, 21}


def test_material_metrics_builder():
    assert brain._material_metrics() == "(no posts with metrics yet)"
    db.set_setting("me", {"username": HANDLE, "followers": 200})
    db.upsert_post(_tweet("met-m1", likes=20, reposts=5, replies=5))
    db.upsert_post(_tweet("met-m2", likes=1))
    text = brain._material_metrics()
    assert "REAL METRICS" in text and "followers 200" in text
    assert "met-m1" in text  # top performer surfaces by follower rate


# ---------------- agent wiring ----------------

def test_learn_loop_uses_metrics_refresh(monkeypatch):
    seen: dict = {}

    async def fake_refresh(x, cfg, limit=60):
        seen["limit"] = limit
        return {"refreshed": 7, "followers": 421, "avg_engagement_rate": 0.12,
                "reflected": True}

    async def fake_reflect(trigger, cfg):
        return "brain: ok"

    monkeypatch.setattr(metrics, "refresh_metrics", fake_refresh)  # module attr: learn() looks it up at call time
    monkeypatch.setattr(agent_mod.voice_mod, "build_voice",
                        lambda cfg, force=False: None)
    monkeypatch.setattr(agent_mod, "_reflect", fake_reflect)

    agent = agent_mod.Agent(Config())
    res = _run(agent.learn())
    assert seen["limit"] == 60
    assert res["refreshed"] == 7
    assert res["metrics"].startswith("7 posts captured")
    assert res["voice"] == "rebuilt" and res["brain"] == "brain: ok"


# ---------------- API shape ----------------

def test_api_growth_top_times_shape():
    db.set_setting("me", {"username": HANDLE, "followers": 100})
    _seed_identity([(1, 90), (0, 100)])
    db.upsert_post(_tweet("met-api", likes=10, reposts=2, replies=3,
                          created_at=datetime.now().isoformat(
                              timespec="seconds")))

    r = client.get("/api/analytics/growth", params={"days": 7})
    assert r.status_code == 200
    g = r.json()
    assert g["days"] == 7 and len(g["series"]) == 7
    day = g["series"][-1]
    assert {"date", "followers", "posts", "avg_engagement_rate", "best_post"} \
        <= set(day)
    assert g["followers_delta"] == 10

    r = client.get("/api/analytics/top", params={"limit": 5, "days": 30})
    assert r.status_code == 200
    top = r.json()["posts"]
    assert top and top[0]["rank"] == 1 and "url" in top[0] and "rate" in top[0]

    r = client.get("/api/analytics/times")
    assert r.status_code == 200
    tm = r.json()
    assert tm["source"] in ("real", "heuristic")
    assert len(tm["hours"]) == 24
    assert all({"hour", "posts", "engagement", "avg_engagement"} <= set(h)
               for h in tm["hours"])
    assert isinstance(tm["best_hours"], list)
