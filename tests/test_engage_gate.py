"""v0.3.8 engage quality gate — hermetic (dryrun X, test DB, no network).

Covers the scoring math (recency decay + hard reject, traction log-scale,
crowding sweet spot, composite weights), filter_targets (threshold, cap 12,
missing-created_at WARN path), and the engage loop integration: the LLM is
never called on rejected targets and the score attaches to draft meta.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import engage_gate, replies                   # noqa: E402

NOW = datetime(2026, 8, 19, 12, 0, 0)


def _tweet(**over) -> dict:
    """A default healthy target: 2h old, rising, sweet-spot crowd, on-niche."""
    base = {
        "x_id": "t1", "author_handle": "someone", "is_own": 0,
        "created_at": (NOW - timedelta(hours=2)).isoformat(timespec="seconds"),
        "text": "hot take about building tools and shipping things",
        "likes": 300, "reposts": 60, "replies": 8, "bookmarks": 10,
        "impressions": 9000,
    }
    base.update(over)
    return base


def _clean_db():
    for t in ("posts", "drafts", "engagements", "agent_log"):
        with db.connect() as c:
            c.execute(f"DELETE FROM {t}")
    db.set_setting("style_profile", None)


def _style_topics(topics: list[str]):
    db.set_setting("style_profile",
                   {"stats": {"topics": topics}})


# ---------------- scoring math ----------------

def test_recency_full_then_linear_decay_to_zero():
    cfg = Config()
    fresh = engage_gate.score_target(cfg, _tweet(), NOW)
    assert fresh.recency == 1.0
    # midpoint of the 3h→24h ramp (13.5h) decays to ~0.5
    mid = engage_gate.score_target(
        cfg, _tweet(created_at=(NOW - timedelta(hours=13.5)).isoformat()), NOW)
    assert abs(mid.recency - 0.5) < 0.02, mid.recency
    day = engage_gate.score_target(
        cfg, _tweet(created_at=(NOW - timedelta(hours=24)).isoformat()), NOW)
    assert day.recency == 0.0 and day.score > 0, \
        "24h = zero recency points, NOT a hard reject"


def test_recency_hard_reject_over_48h():
    cfg = Config()
    stale = engage_gate.score_target(
        cfg, _tweet(created_at=(NOW - timedelta(hours=49)).isoformat()), NOW)
    assert stale.score == 0
    assert any("hard reject" in r for r in stale.reasons)
    kept, rejected = engage_gate.filter_targets(
        cfg, [_tweet(created_at=(NOW - timedelta(hours=72)).isoformat(),
                     x_id="dead")], NOW)
    assert kept == [] and len(rejected) == 1


def test_missing_created_at_warns_not_rejects():
    _clean_db()
    cfg = Config()
    ts = engage_gate.score_target(cfg, _tweet(created_at=None), NOW)
    assert ts.age_h is None
    assert ts.recency == 0.5, "unknown age → neutral, never a guess"
    assert ts.score > 0, "missing created_at must not hard-reject"
    assert any("WARN" in r for r in ts.reasons)
    # the WARN surfaces in the log for the debug view
    engage_gate.filter_targets(cfg, [_tweet(x_id="n1", created_at=None)], NOW)
    with db.connect() as c:
        rows = c.execute(
            "SELECT level FROM agent_log WHERE loop='engage' "
            "AND message LIKE '%missing created_at%'").fetchall()
    assert rows and rows[0]["level"] == "warn"


def test_traction_log_scale_monotonic():
    cfg = Config()
    zero = engage_gate.score_target(cfg, _tweet(likes=0, reposts=0), NOW)
    assert zero.traction == 0.0 and any("no traction" in r for r in zero.reasons)
    small = engage_gate.score_target(cfg, _tweet(likes=30, reposts=5), NOW)
    full = engage_gate.score_target(cfg, _tweet(likes=600, reposts=150), NOW)
    mega = engage_gate.score_target(cfg, _tweet(likes=50000, reposts=9000), NOW)
    assert small.traction > zero.traction
    assert abs(full.traction - 1.0) < 0.01, "500 combined ≈ full points"
    assert mega.traction == 1.0, "saturates — a mega-viral tweet isn't 10x better"


def test_crowding_sweet_spot():
    cfg = Config()
    lonely = engage_gate.score_target(cfg, _tweet(replies=0), NOW)
    sweet = engage_gate.score_target(cfg, _tweet(replies=8), NOW)
    busy = engage_gate.score_target(cfg, _tweet(replies=60), NOW)
    mob = engage_gate.score_target(cfg, _tweet(replies=400), NOW)
    assert sweet.crowding == 1.0, "1-15 replies is the sweet spot"
    assert lonely.crowding < sweet.crowding and lonely.crowding >= 0.7, \
        "0 replies is slightly bad, not fatal"
    assert busy.crowding < sweet.crowding, "mild penalty past the sweet spot"
    assert mob.crowding >= engage_gate.CROWD_FLOOR, "penalty is floored (mild)"


def test_composite_matches_configured_weights():
    _clean_db()
    cfg = Config()
    cfg.agent.engage_gate = {
        "weights": {"recency": 0.5, "traction": 0.5},  # override defaults
        "threshold": 10, "max_targets": 12}
    tweet = _tweet(created_at=None, likes=0, reposts=0, replies=8)
    # recency .5 (unknown), traction 0, everything else must not leak in
    ts = engage_gate.score_target(cfg, tweet, NOW)
    assert ts.score == 25, \
        "renormalized weights: 100 * (.5*.5 + .5*0) / (.5+.5) == 25"
    # default weights: hand-check the weighted sum
    cfg2 = Config()
    t2 = _tweet(created_at=None, replies=8)
    ts2 = engage_gate.score_target(cfg2, t2, NOW)
    w = engage_gate.gate_cfg(cfg2)["weights"]
    expected = 100 * (w["recency"] * 0.5 + w["traction"] * ts2.traction +
                      w["author"] * 0.5 + w["crowding"] * 1.0 +
                      w["fit"] * ts2.niche_fit) / sum(w.values())
    assert ts2.score == round(expected)


def test_filter_targets_threshold_sort_and_cap():
    _clean_db()
    _style_topics(["building", "tools", "shipping"])
    cfg = Config()
    batch = [_tweet(x_id=f"t{i}",
                    created_at=(NOW - timedelta(hours=h)).isoformat(),
                    likes=likes)
             for i, (h, likes) in enumerate(
                 [(1, 400), (2, 300), (5, 250), (4, 200), (20, 500),
                  (30, 90), (47, 60), (2, 20), (3, 15), (6, 10),
                  (10, 8), (8, 6), (12, 5), (9, 4), (26, 3)])
             ] + \
            [_tweet(x_id=f"s{i}",
                    created_at=(NOW - timedelta(hours=50 + i)).isoformat())
             for i in range(4)]
    kept, rejected = engage_gate.filter_targets(cfg, batch, NOW)
    assert len(kept) <= 12, "cap at 12"
    scores = [s.score for _, s in kept]
    assert scores == sorted(scores, reverse=True), "sorted by score desc"
    assert all(s.score >= 55 for _, s in kept)
    assert all(s.score < 55 for _, s in rejected)
    assert len(kept) + len(rejected) == len(batch)
    assert all(s.score == 0 for _, s in rejected if s.age_h and s.age_h > 48)


def test_niche_fit_uses_brain_map():
    _clean_db()
    cfg = Config()
    _style_topics(["rust", "compilers", "linkers"])
    on = engage_gate.score_target(cfg, _tweet(text="the borrow checker "
                                                "taught me rust patience"), NOW)
    off = engage_gate.score_target(cfg, _tweet(text="great soup recipes "
                                                 "for winter evenings"), NOW)
    assert on.niche_fit > 0 and on.niche_fit <= 1.0
    assert off.niche_fit == 0.0 and any("niche" in r for r in off.reasons)
    # no niche map at all (no topics, no themes) → neutral, not punishing
    db.set_setting("style_profile", None)
    cfg.agent.evergreen_themes = []
    neutral = engage_gate.score_target(cfg, _tweet(text="anything"), NOW)
    assert neutral.niche_fit == 0.5


def test_author_surface_from_db_stats():
    _clean_db()
    cfg = Config()
    unknown = engage_gate.score_target(cfg, _tweet(author_handle="stranger_9"), NOW)
    assert unknown.author_surface == 0.5, "unseen author → neutral"
    for i in range(3):  # this author's posts are already in our corpus
        db.upsert_post({"x_id": f"a{i}", "author_handle": "chatty", "is_own": 0,
                        "created_at": NOW.isoformat(), "text": "engages back",
                        "likes": 150, "reposts": 40, "replies": 25,
                        "impressions": 5000})
    seen = engage_gate.score_target(cfg, _tweet(author_handle="chatty"), NOW)
    assert seen.author_surface > 0.5, "corpus history raises the surface score"


# ---------------- engage loop integration ----------------

def test_engage_loop_gates_before_llm_and_attaches_meta(monkeypatch):
    _clean_db()
    _style_topics(["building", "tools", "shipping"])
    cfg = Config()
    # seed the niche pool: one great target + three dead ones
    db.upsert_post({"x_id": "live1", "author_handle": "poster", "is_own": 0,
                    "created_at": (datetime.now() - timedelta(hours=1)).isoformat(
                        timespec="seconds"),
                    "text": "thoughts on building tools and shipping things?",
                    "likes": 350, "reposts": 80, "replies": 10,
                    "impressions": 12000})
    for i, age in enumerate((72, 100, 200)):
        db.upsert_post({"x_id": f"dead{i}", "author_handle": f"ghost{i}",
                        "is_own": 0,
                        "created_at": (datetime.now() -
                                       timedelta(hours=age)).isoformat(
                            timespec="seconds"),
                        "text": "old conversation about tools",
                        "likes": 20, "reposts": 2, "replies": 1,
                        "impressions": 800})

    calls: list[str] = []

    def spy_chat(cfg2, system, user, **kw):
        calls.append(user[:60])
        return '{"reply": "sharp angle — answered with a number, no fluff"}'

    monkeypatch.setattr(replies, "chat", spy_chat)

    ids = replies.draft_niche_replies(cfg, limit=3)
    # every seeded dead target was rejected BEFORE any LLM call — only the
    # live one is worth the (fake) budget
    assert len(calls) == 1 and "poster" in calls[0], calls
    assert len(ids) == 1
    d = db.get_draft(ids[0])
    assert d["meta"]["reply_to_x_id"] == "live1"
    ts = d["meta"]["target_score"]
    assert ts["score"] >= 55 and ts["components"]["recency"] > 0.9
    assert ts["verdict"] in ("fresh", "rising")
    # rejections are logged for the Insights/debug view
    with db.connect() as c:
        rows = c.execute("SELECT message FROM agent_log WHERE loop='engage' "
                         "AND message LIKE 'gate: rejected%'").fetchall()
    assert rows and "3/4" in rows[0]["message"]


def test_engage_loop_all_rejected_calls_no_llm(monkeypatch):
    _clean_db()
    cfg = Config()
    db.upsert_post({"x_id": "dead9", "author_handle": "ghost", "is_own": 0,
                    "created_at": (datetime.now() - timedelta(hours=90)).isoformat(
                        timespec="seconds"),
                    "text": "long dead thread",
                    "likes": 5, "reposts": 0, "replies": 0, "impressions": 100})

    def boom(cfg2, *a, **kw):  # noqa: ANN001 — must never run
        raise AssertionError("LLM called on a rejected target")

    monkeypatch.setattr(replies, "chat", boom)
    ids = replies.draft_niche_replies(cfg, limit=3)
    assert ids == []


def test_meta_is_json_safe():
    _clean_db()
    cfg = Config()
    ts = engage_gate.score_target(cfg, _tweet(), NOW)
    import json
    m = json.loads(json.dumps(ts.meta()))  # round-trips through the drafts table
    assert m["score"] == ts.score and "components" in m and m["age_h"] == 2.0
