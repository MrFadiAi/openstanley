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
    assert day.recency == 0.0 and day.score == 0, \
        "24h = the hard wall — no traction/fit can buy a day-old thread"


def test_recency_hard_reject_over_24h():
    cfg = Config()
    stale = engage_gate.score_target(
        cfg, _tweet(created_at=(NOW - timedelta(hours=25)).isoformat()), NOW)
    assert stale.score == 0
    assert any("hard reject" in r for r in stale.reasons)
    kept, rejected = engage_gate.filter_targets(
        cfg, [_tweet(created_at=(NOW - timedelta(hours=72)).isoformat(),
                     x_id="dead")], NOW)
    assert kept == [] and len(rejected) == 1
    # the leak the old gate had: 24-48h old + hot on other axes used to pass
    hot_old = _tweet(created_at=(NOW - timedelta(hours=30)).isoformat(),
                     replies=200, likes=5000, x_id="hot-but-old")
    s = engage_gate.score_target(cfg, hot_old, NOW)
    assert s.score == 0, "no score can rescue a 30h-old conversation"


def test_missing_created_at_hard_rejects():
    _clean_db()
    cfg = Config()
    ts = engage_gate.score_target(cfg, _tweet(created_at=None), NOW)
    assert ts.age_h is None
    assert ts.score == 0, "unknown age is treated as old, never neutral"
    assert any("hard reject" in r for r in ts.reasons)
    kept, rejected = engage_gate.filter_targets(
        cfg, [_tweet(x_id="n1", created_at=None)], NOW)
    assert kept == [] and len(rejected) == 1


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
    tweet = _tweet(created_at=(NOW - timedelta(hours=1)).isoformat(),
                   likes=0, reposts=0, replies=8)
    # fresh (recency 1.0), traction 0, other factors must not leak in
    ts = engage_gate.score_target(cfg, tweet, NOW)
    assert ts.score == 50, \
        "configured weights: 100 * (.5*1 + .5*0) == 50 — unweighted factors stay out"
    # default weights: hand-check the weighted sum on a KNOWN-age tweet
    cfg2 = Config()
    t2 = _tweet(created_at=(NOW - timedelta(hours=2)).isoformat(), replies=8)
    ts2 = engage_gate.score_target(cfg2, t2, NOW)
    w = engage_gate.gate_cfg(cfg2)["weights"]
    expected = 100 * (w["recency"] * ts2.recency + w["traction"] * ts2.traction +
                      w["author"] * ts2.author_surface +
                      w["crowding"] * ts2.crowding +
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
    # fresh-first picker (2026-08-26): dead targets are filtered BEFORE
    # the gate, so it no longer logs "3/4" — none may reach the gate at
    # all. The LLM-budget proof is len(calls) == 1 above.
    assert all("dead" not in r["message"] for r in rows)


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


# ---------------- v0.5.1: per-author batch crowding cap ----------------

def test_niche_batch_caps_two_replies_per_author(monkeypatch):
    """The user's complaint: 5 drafts to @naval in ONE batch = spammy. The
    cap keeps at most PER_AUTHOR_BATCH_CAP per author, walking past a
    crowded author to fill the batch from the rest of the pool."""
    _clean_db()
    _style_topics(["building", "tools", "shipping"])
    cfg = Config()
    now = datetime.now()
    # five healthy targets from ONE author + two from others
    for i in range(5):
        db.upsert_post({"x_id": f"naval{i}", "author_handle": "naval",
                        "is_own": 0,
                        "created_at": (now - timedelta(hours=1)).isoformat(
                            timespec="seconds"),
                        "text": f"naval take {i} on building tools",
                        "likes": 350, "reposts": 80, "replies": 10,
                        "impressions": 12000})
    for j, who in enumerate(("alice", "bob")):
        db.upsert_post({"x_id": f"{who}x", "author_handle": who, "is_own": 0,
                        "created_at": (now - timedelta(hours=1)).isoformat(
                            timespec="seconds"),
                        "text": f"{who} on shipping tools and building",
                        "likes": 300, "reposts": 60, "replies": 8,
                        "impressions": 9000})

    def ok_llm(cfg2, system, user, **kw):
        return '{"reply": "sharp angle — answered with a number, no fluff"}'

    monkeypatch.setattr(replies, "chat", ok_llm)
    ids = replies.draft_niche_replies(cfg, limit=5)
    assert len(ids) == 4                       # 2 naval + alice + bob
    authors = [db.get_draft(i)["meta"]["target_author"] for i in ids]
    assert authors.count("naval") == replies.PER_AUTHOR_BATCH_CAP
    assert "alice" in authors and "bob" in authors


def test_mention_batch_caps_two_replies_per_author(monkeypatch):
    _clean_db()
    cfg = Config()
    with db.connect() as c:                    # five fresh mentions from ONE author
        for i in range(5):
            c.execute(
                "INSERT INTO engagements (account_id, x_id, kind, author_handle, "
                "author_name, text, status, created_at, seen_at) "
                "VALUES (1, ?, 'mention', 'chatter', 'Chatter', ?, 'new', ?, ?)",
                (f"m{i}", f"hey about those tools ({i})",
                 (datetime.now() - timedelta(minutes=10)).isoformat(
                     timespec="seconds"),
                 datetime.now().isoformat(timespec="seconds")))

    def ok_llm(cfg2, system, user, **kw):
        return '{"reply": "sharp angle — answered with a number, no fluff"}'

    monkeypatch.setattr(replies, "chat", ok_llm)
    ids = replies.draft_replies(cfg, limit=8)
    assert len(ids) == replies.PER_AUTHOR_BATCH_CAP
    with db.connect() as c:                    # the rest stay 'new' for retry
        (still,) = c.execute(
            "SELECT COUNT(*) FROM engagements WHERE status='new'").fetchone()
    assert int(still) == 3


def test_meta_is_json_safe():
    _clean_db()
    cfg = Config()
    ts = engage_gate.score_target(cfg, _tweet(), NOW)
    import json
    m = json.loads(json.dumps(ts.meta()))  # round-trips through the drafts table
    assert m["score"] == ts.score and "components" in m and m["age_h"] == 2.0


def test_niche_reply_empty_json_logs_and_continues(monkeypatch):
    """2026-08-28 13:30: 4 gate-passing targets, 0 drafts, 0 logs — the
    empty-reply continue swallowed the failure invisibly. It must log the
    json keys it actually got."""
    import asyncio
    from openstanley.gen import replies as rmod
    from openstanley.core import db as _db

    from datetime import datetime, timedelta
    fresh = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    monkeypatch.setattr(rmod, "_pick_niche_targets",
                        lambda cfg, limit=9, acct=None: [
                            {"x_id": "z1", "author_handle": "zauthor",
                             "text": "a very interesting technical post about "
                                     "shipping things with real detail",
                             "likes": 40, "created_at": fresh}])
    monkeypatch.setattr(rmod, "brain_mod", type("B", (), {
        "brain_context": staticmethod(lambda *a, **k: "")})())
    monkeypatch.setattr(rmod, "chat", lambda *a, **k: '{"answer": "nothing"}')
    logs = []
    monkeypatch.setattr(_db, "log", lambda loop, msg, level="info":
                        logs.append((loop, msg, level)))
    out = rmod.draft_niche_replies(None if False else __import__(
        "openstanley.core.config", fromlist=["Config"]).Config())
    assert out == []
    assert any("EMPTY" in m and "zauthor" in m for _l, m, _lv in logs), logs
