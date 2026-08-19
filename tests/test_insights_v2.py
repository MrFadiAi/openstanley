"""Insights v2 — every aggregate is computed from real rows, zero-safe."""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.gen import insights as ins                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402
from openstanley.server.__main__ import app                        # noqa: E402


def _seed_posts(rows, text=''):
    with db.connect() as c:
        c.execute("DELETE FROM posts")
        for x_id, created, imp, likes, eng in rows:
            c.execute("INSERT INTO posts (account_id, x_id, is_own, created_at, "
                      "text, impressions, likes, engagement) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (1, x_id, 1, created, text, imp, likes, eng))


def test_heatmap_year_of_days_and_totals():
    today = date.today()
    _seed_posts([
        ("a1", f"{today.isoformat()} 10:00:00", 100, 2, 3),
        ("a2", f"{(today - timedelta(days=40)).isoformat()} 10:00:00", 250, 0, 0),
    ])
    hm = ins.heatmap()
    assert len(hm) == 365
    assert {c["value"] for c in hm} >= {100, 250}
    assert sum(c["value"] for c in hm) == 350


def test_growth_delta_vs_previous_window():
    today = date.today()
    _seed_posts([
        ("g1", f"{today.isoformat()} 10:00:00", 100, 0, 0),                       # current
        ("g2", f"{(today - timedelta(days=45)).isoformat()} 10:00:00", 400, 0, 0),  # previous window
    ])
    g = ins.growth(days=30)
    assert g["total"] == 100 and g["prev_total"] == 400
    assert g["delta_pct"] == -75.0


def test_growth_zero_history_is_none_not_zero_pct():
    _seed_posts([])
    g = ins.growth(days=30)
    assert g["total"] == 0 and g["prev_total"] == 0 and g["delta_pct"] is None


def test_milestones_unlock_against_real_numbers():
    _seed_posts([("m1", "2026-08-19 10:00:00", 5000, 10, 12)])
    ms = {m["id"]: m for m in ins.milestones()}
    assert ms["imp_1k"]["unlocked"] is True
    assert ms["imp_100k"]["unlocked"] is False
    assert ms["imp_1k"]["current"] == 5000


def test_type_performance_joins_draft_image():
    _seed_posts([
        ("t1", "2026-08-19 10:00:00", 3000, 30, 42),
        ("t2", "2026-08-19 11:00:00", 1000, 10, 15),
    ])
    d = db.add_draft(text="with media", acct=1)
    db.update_draft(d, status="published", x_id="t2", acct=1)  # t2 ← image post
    with db.connect() as c:
        c.execute("UPDATE drafts SET image='media_x.png' WHERE id=?", (d,))
    tp = ins.type_performance()
    assert tp["text"]["count"] == 1 and tp["text"]["avg_impressions"] == 3000
    assert tp["image"]["count"] == 1 and tp["image"]["avg_impressions"] == 1000
    assert tp["ratio"] == 3.0


def test_best_content_orders_by_engagement_rate():
    _seed_posts([
        ("b1", "2026-08-19 10:00:00", 10000, 5, 10),   # 0.1%
        ("b2", "2026-08-18 10:00:00", 500, 5, 20),     # 4% — the winner
        ("b3", "2026-08-17 10:00:00", 2000, 2, 3),     # 0.15%
    ])
    best = ins.best_content()
    assert best[0]["x_id"] == "b2"
    assert [b["x_id"] for b in best] == ["b2", "b3", "b1"]


def test_overview_endpoint_shape():
    _seed_posts([("o1", date.today().isoformat(), 100, 1, 2)])
    with TestClient(app) as client:
        r = client.get("/api/insights/overview")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"account", "heatmap", "growth", "orbit",
                         "milestones", "types", "best"}
    assert body["account"]["today_impressions"] == 100


def test_repost_creates_approval_gated_draft():
    _seed_posts([("r1", "2026-08-19 10:00:00", 900, 9, 18)], text="repostable")
    with TestClient(app) as client:
        r = client.post("/api/posts/r1/repost")
    assert r.status_code == 200, r.text
    did = r.json()["draft_id"]
    d = db.get_draft(did)
    assert d["status"] == "draft"                 # never auto-published
    assert d["meta"]["source"] == "repost"
    assert d["meta"]["original_x_id"] == "r1"


def test_repost_unknown_post_404():
    with TestClient(app) as client:
        r = client.post("/api/posts/nope/repost")
    assert r.status_code == 404
