"""Mention inbox v0.3.9 — hermetic (dryrun X, test DB, fake LLM, no network).

Covers: normalize + own-skip + dedupe (against posts AND seen_mentions),
the pending query (newest-first, handled filter), handled-marking exactly
when a draft exists, conversation-parent context reaching the LLM prompt,
engage-gate bypass (target_score None), the full mentions loop on dryrun X,
the API shape, on-demand drafting, and the autopilot rotation including
the mentions phase.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"  # no cron loop inside tests

from openstanley.core import db                                    # noqa: E402
db.init_db()

from fastapi.testclient import TestClient                       # noqa: E402

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import autopilot as ap                        # noqa: E402
from openstanley.gen import mentions as mm                         # noqa: E402
from openstanley.gen.agent import Agent                            # noqa: E402
import openstanley.server.__main__ as server                       # noqa: E402

client = TestClient(server.app)

REPLY_JSON = '{"reply": "great question — the ugly version first, always."}'


def _clean_db():
    for t in ("seen_mentions", "drafts", "engagements", "posts", "agent_log"):
        with db.connect() as c:
            c.execute(f"DELETE FROM {t}")
    db.set_setting("me", {"username": "orbexai"})


class FakeX:
    """XClient-shaped stub: canned mentions + parent lookup, no network."""

    mode = "test"

    def __init__(self, rows: list[dict]):
        self.rows = rows

    async def mentions(self, limit: int = 30) -> list[dict]:
        return self.rows[:limit]

    async def get_tweet(self, x_id: str) -> dict:
        return {"x_id": x_id, "text": f"parent text for {x_id}",
                "author": "orbexai"}  # replies to us

    async def me(self) -> dict:
        return {"username": "orbexai"}


def _mention(x_id: str, author: str = "someone", text: str = "hey what stack?",
             **over) -> dict:
    base = {"x_id": x_id, "author_handle": author, "text": text,
            "created_at": datetime.now().isoformat(timespec="seconds")}
    base.update(over)
    return base


def _fake_llm(monkeypatch, reply: str = REPLY_JSON) -> list[str]:
    """Stub mm.chat; returns the captured user prompts (spy)."""
    calls: list[str] = []

    def spy(cfg, system, user, **kw):
        calls.append(user)
        return reply

    monkeypatch.setattr(mm, "chat", spy)
    return calls


import asyncio  # noqa: E402 — used by the helpers below


def _fetch(x, limit: int = 30) -> list[dict]:
    return asyncio.run(mm.fetch_mentions(x, limit=limit))


# ---------------- normalize / dedupe / own-skip ----------------

def test_fetch_normalizes_fields():
    _clean_db()
    raw = [_mention("1790000000000000001", author="alice",
                    text="love this agent, what model runs it?")]
    stored = _fetch(FakeX(raw))
    assert len(stored) == 1
    m = stored[0]
    assert m["author"] == "alice" and m["x_id"] == "1790000000000000001"
    assert m["tweet_link"] == "https://x.com/alice/status/1790000000000000001"
    assert m["conversation_id"] == "1790000000000000001"
    assert m["reply_to_me"] == 0
    # persisted with the exact spec columns
    with db.connect() as c:
        row = c.execute("SELECT * FROM seen_mentions").fetchone()
    assert row["author"] == "alice" and row["handled"] == 0 and row["first_seen"]


def test_fetch_skips_own_tweets():
    _clean_db()
    raw = [
        _mention("1790000000000000010", author="orbexai", text="my own echo"),
        _mention("1790000000000000011", author="bob", text="real mention"),
    ]
    stored = _fetch(FakeX(raw))
    authors = {m["author"] for m in stored}
    assert "orbexai" not in authors, "own tweets never enter the inbox"
    assert "bob" in authors


def test_fetch_dedupes_against_own_posts_table():
    _clean_db()
    # an x_id already imported as OUR post is skipped even with a foreign author
    db.upsert_post({"x_id": "1790000000000000012", "author_handle": "orbexai",
                    "is_own": 1, "text": "x", "created_at": None})
    stored = _fetch(FakeX([_mention("1790000000000000012", author="stranger",
                                    text="hi")]))
    assert stored == [], "x_id already known as an own post is deduped"


def test_fetch_dedupes_against_seen_mentions():
    _clean_db()
    x = FakeX([_mention("1790000000000000020", author="amy", text="first"),
               _mention("1790000000000000021", author="ben", text="second")])
    first = _fetch(x)
    assert len(first) == 2
    # same batch again → nothing new, nothing duplicated
    second = _fetch(x)
    assert second == []
    with db.connect() as c:
        (n,) = c.execute("SELECT COUNT(*) FROM seen_mentions").fetchone()
    assert n == 2
    # one new + one old → only the new one returns
    x.rows.append(_mention("1790000000000000022", author="cid", text="third"))
    third = _fetch(x)
    assert [m["author"] for m in third] == ["cid"]


def test_fetch_enriches_parent_context_when_reply_to_me():
    _clean_db()
    raw = [_mention("1790000000000000030", author="dana",
                    text="this thread nailed it",
                    in_reply_to_x_id="1789000000000000099")]
    stored = _fetch(FakeX(raw))
    assert len(stored) == 1
    m = stored[0]
    assert m["reply_to_me"] == 1, "parent author == me → reply_to_me"
    assert m["parent_text"] == "parent text for 1789000000000000099"
    # a mention explicitly flagged reply_to_me but with no parent id → kept, no parent text
    raw2 = [_mention("1790000000000000031", author="eli", text="so true",
                     reply_to_me=1)]
    m2 = _fetch(FakeX(raw2))[0]
    assert m2["reply_to_me"] == 1 and not m2.get("parent_text")


def test_fetch_failure_is_logged_not_raised():
    _clean_db()

    class BrokenX(FakeX):
        async def mentions(self, limit: int = 30) -> list[dict]:
            raise RuntimeError("x exploded")

    assert _fetch(BrokenX([])) == []
    with db.connect() as c:
        row = c.execute("SELECT level FROM agent_log WHERE loop='mentions' "
                        "AND message LIKE 'fetch failed%'").fetchone()
    assert row and row["level"] == "error"


# ---------------- pending query ----------------

def test_pending_mentions_newest_first_handled_filter():
    _clean_db()
    now = datetime.now()
    for i, hours in enumerate((5, 1, 3)):  # inserted out of order
        with db.connect() as c:
            c.execute(
                "INSERT INTO seen_mentions (x_id, author, text, created_at, first_seen, handled) "
                "VALUES (?,?,?,?,?,?)",
                (f"17900000000000001{i:02d}", f"u{i}", f"m{i}",
                 (now - timedelta(hours=hours)).isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds"), 1 if i == 1 else 0),
            )
    pending = mm.pending_mentions()
    assert [p["x_id"] for p in pending] == ["1790000000000000102",
                                            "1790000000000000100"], \
        "handled excluded, newest first"
    # cap honored
    assert len(mm.pending_mentions(limit=1)) == 1


# ---------------- drafting ----------------

def test_draft_creates_reply_and_marks_handled(monkeypatch):
    _clean_db()
    calls = _fake_llm(monkeypatch)
    m = {"x_id": "1790000000000000200", "author": "frank",
         "text": "what's your take on agents?", "reply_to_me": 0}
    did = mm.draft_mention_reply(Config(), m)
    assert did
    d = db.get_draft(did)
    assert d["kind"] == "reply" and d["status"] == "draft", "approval-gated"
    assert d["meta"]["source"] == "mention"
    assert d["meta"]["reply_to_x_id"] == "1790000000000000200"
    assert d["meta"]["target_author"] == "frank"
    assert d["meta"]["target_score"] is None, "mention replies bypass the gate"
    assert "frank" in calls[0] and "what's your take" in calls[0]


def test_draft_marks_handled_exactly_when_draft_exists(monkeypatch):
    _clean_db()
    with db.connect() as c:
        c.execute("INSERT INTO seen_mentions (x_id, author, text, created_at, first_seen, handled) "
                  "VALUES ('1790000000000000300','gus','ping','2026-08-19T10:00:00',"
                  "'2026-08-19T10:00:00',0)")
    # LLM fails → no draft, NOT handled, retried next run
    def boom(cfg, system, user, **kw):
        raise mm.LLMError("llm down")

    monkeypatch.setattr(mm, "chat", boom)
    assert mm.draft_mention_reply(Config(), {
        "x_id": "1790000000000000300", "author": "gus", "text": "ping"}) is None
    with db.connect() as c:
        (handled,) = c.execute("SELECT handled FROM seen_mentions "
                               "WHERE x_id='1790000000000000300'").fetchone()
    assert handled == 0
    # empty reply → still not handled
    _fake_llm(monkeypatch, reply='{"reply": ""}')
    assert mm.draft_mention_reply(Config(), {
        "x_id": "1790000000000000300", "author": "gus", "text": "ping"}) is None
    with db.connect() as c:
        (handled,) = c.execute("SELECT handled FROM seen_mentions "
                               "WHERE x_id='1790000000000000300'").fetchone()
    assert handled == 0
    # success → handled
    _fake_llm(monkeypatch)
    assert mm.draft_mention_reply(Config(), {
        "x_id": "1790000000000000300", "author": "gus", "text": "ping"})
    with db.connect() as c:
        (handled,) = c.execute("SELECT handled FROM seen_mentions "
                               "WHERE x_id='1790000000000000300'").fetchone()
    assert handled == 1


def test_draft_prompt_contains_conversation_parent(monkeypatch):
    _clean_db()
    calls = _fake_llm(monkeypatch)
    parent = "we shipped the ugly version first and it taught us everything"
    m = {"x_id": "1790000000000000400", "author": "hana",
         "text": "this is exactly right", "reply_to_me": 1, "parent_text": parent}
    assert mm.draft_mention_reply(Config(), m)
    assert parent in calls[0], "parent post text must reach the LLM"
    assert "OUR post" in calls[0]
    # without reply_to_me the parent context is dropped
    calls.clear()
    m2 = {**m, "x_id": "1790000000000000401", "reply_to_me": 0}
    assert mm.draft_mention_reply(Config(), m2)
    assert parent not in calls[0]


# ---------------- loop integration (dryrun X + fake LLM) ----------------

def test_mentions_loop_dryrun_end_to_end(monkeypatch):
    _clean_db()
    _fake_llm(monkeypatch)
    cfg = Config()
    agent = Agent(cfg)
    res = asyncio.run(agent.mentions())
    assert res["mentions_new"] == 3, "dryrun client serves 3 sample mentions"
    assert res["replies_drafted"] == 3, "default budget mention_drafts_per_run=3"
    assert mm.pending_mentions() == [], "all drafted → handled"
    replies = db.drafts_by_status("draft", 50)
    mention_replies = [d for d in replies
                       if (d.get("meta") or {}).get("source") == "mention"]
    assert len(mention_replies) == 3
    assert all(d["kind"] == "reply" for d in mention_replies)
    # second run: dedupe means nothing new, nothing left pending
    res2 = asyncio.run(agent.mentions())
    assert res2["mentions_new"] == 0 and res2["replies_drafted"] == 0


def test_mentions_loop_budget_caps_drafts(monkeypatch):
    _clean_db()
    _fake_llm(monkeypatch)
    cfg = Config()
    cfg.agent.mention_drafts_per_run = 1
    agent = Agent(cfg)
    res = asyncio.run(agent.mentions())
    assert res["replies_drafted"] == 1
    assert len(mm.pending_mentions()) == 2, "rest wait for the next run"


# ---------------- API ----------------

def test_api_mentions_shape_and_pending_filter():
    _clean_db()
    now = datetime.now()
    with db.connect() as c:
        c.execute("INSERT INTO seen_mentions (x_id, author, text, created_at, first_seen, handled) "
                  "VALUES ('1790000000000000500','ivy','question A',?, ?, 0)",
                  (now.isoformat(timespec="seconds"),
                   now.isoformat(timespec="seconds")))
    did = db.add_draft(text="reply to handled one", kind="reply",
                       meta={"source": "mention",
                             "reply_to_x_id": "1790000000000000501"})
    with db.connect() as c:
        c.execute("INSERT INTO seen_mentions (x_id, author, text, created_at, first_seen, handled) "
                  "VALUES ('1790000000000000501','jack','question B',?, ?, 1)",
                  (now.isoformat(timespec="seconds"),
                   now.isoformat(timespec="seconds")))
    r = client.get("/api/mentions", params={"pending": 1})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [m["x_id"] for m in rows] == ["1790000000000000500"]
    row = rows[0]
    assert set(row) >= {"x_id", "author", "text", "created_at", "first_seen",
                        "handled", "tweet_link", "conversation_id",
                        "reply_to_me", "draft"}
    assert row["author"] == "ivy" and row["draft"] is None
    # non-pending view carries the draft status join
    all_rows = client.get("/api/mentions", params={"pending": 0}).json()
    by_id = {m["x_id"]: m for m in all_rows}
    assert by_id["1790000000000000501"]["draft"] == {"id": did, "status": "draft"}
    db.update_draft(did, status="rejected")  # cleanup


def test_api_mentions_draft_button_and_404(monkeypatch):
    _clean_db()
    _fake_llm(monkeypatch)
    with db.connect() as c:
        c.execute("INSERT INTO seen_mentions (x_id, author, text, created_at, first_seen, handled) "
                  "VALUES ('1790000000000000600','kate','draft me',"
                  "'2026-08-19T10:00:00','2026-08-19T10:00:00',0)")
    r = client.post("/api/mentions/1790000000000000600/draft")
    assert r.status_code == 200, r.text
    did = r.json()["draft_id"]
    d = db.get_draft(did)
    assert d["meta"]["source"] == "mention"
    with db.connect() as c:
        (handled,) = c.execute("SELECT handled FROM seen_mentions "
                               "WHERE x_id='1790000000000000600'").fetchone()
    assert handled == 1
    assert client.post("/api/mentions/does-not-exist/draft").status_code == 404


def test_api_loops_mentions_endpoint(monkeypatch):
    _clean_db()
    _fake_llm(monkeypatch)
    r = client.post("/api/loops/mentions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["loop"] == "mentions"
    assert body["result"]["replies_drafted"] >= 1
    assert client.post("/api/loops/bogus").status_code == 404


# ---------------- autopilot rotation ----------------

def test_autopilot_rotation_includes_mentions_phase():
    assert ap.PHASES == ("study", "create", "engage", "mentions", "learn"), \
        "round-robin grows to 5 phases"
    assert "publish" not in ap.PHASES, "publish stays excluded"
    assert [ap.next_phase(i) for i in range(10)] == [
        "study", "create", "engage", "mentions", "learn",
        "study", "create", "engage", "mentions", "learn"]


def test_autopilot_mentions_phase_leaves_drafts_when_autoapprove_off():
    _clean_db()

    class MentionAgent:
        def __init__(self):
            self.calls = 0

        async def mentions(self):
            self.calls += 1
            self.did = db.add_draft(text="mention reply draft", kind="reply",
                                    meta={"source": "mention",
                                          "reply_to_x_id": "1790000000000000700"})
            return {"mentions_new": 1, "replies_drafted": 1}

    _reset_state = db.get_setting(ap.SETTING_KEY)
    db.set_setting(ap.SETTING_KEY, None)
    cfg = Config()
    cfg.agent.auto_approve_replies = False  # default: human gate
    agent = MentionAgent()
    # rotate until mentions is next (fresh agents burn the other phases)
    while ap.next_phase(ap.get_state()["ticks"]) != "mentions":
        asyncio.run(ap.run_tick(MentionAgent(), cfg))
    res = asyncio.run(ap.run_tick(agent, cfg))
    try:
        assert res["ok"] and res["phase"] == "mentions" and agent.calls == 1
        assert res["result"]["approved_replies"] == 0
        assert db.get_draft(agent.did)["status"] == "draft", \
            "mention reply waits in the Inbox"
    finally:
        db.set_setting(ap.SETTING_KEY, _reset_state)
