"""Tool-call registry — intent parsing, time parsing, execution, chat wiring."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openstanley.core import db                      # noqa: E402
db.init_db()

from openstanley.gen import chat, tools              # noqa: E402

FENCE = "`" * 3


def test_parse_when_formats():
    now = datetime(2026, 8, 18, 14, 0)
    assert tools.parse_when("9pm", now) == "2026-08-18T21:00:00"
    assert tools.parse_when("9:30pm", now) == "2026-08-18T21:30:00"
    assert tools.parse_when("tomorrow 9am", now) == "2026-08-19T09:00:00"
    assert tools.parse_when("in 2 hours", now) == "2026-08-18T16:00:00"
    assert tools.parse_when("in 30 minutes", now) == "2026-08-18T14:30:00"
    assert tools.parse_when("18:30", now) == "2026-08-18T18:30:00"
    assert tools.parse_when("tonight 8", now) == "2026-08-18T20:00:00"
    assert tools.parse_when("2026-08-20T10:00:00", now) == "2026-08-20T10:00:00"
    assert tools.parse_when("", now) is None
    assert tools.parse_when("sometime", now) is None
    assert tools.parse_when("99pm", now) is None


def test_parse_actions():
    reply = (f"Queued it.\n{FENCE}action\n"
             '{"tool": "schedule_draft", "args": {"text": "hello post", "when": "9pm"}}\n'
             f"{FENCE}\nmore prose")
    acts = tools.parse_actions(reply)
    assert len(acts) == 1
    assert acts[0]["tool"] == "schedule_draft"
    assert acts[0]["args"]["when"] == "9pm"
    stripped = tools.strip_actions(reply)
    assert "schedule_draft" not in stripped and FENCE + "action" not in stripped
    assert "Queued it." in stripped
    # unknown tools + malformed JSON ignored
    bad = f"{FENCE}action\n{{\"tool\": \"nope\"}}\n{FENCE} {FENCE}action\nnot json\n{FENCE}"
    assert tools.parse_actions(bad) == []


def test_schedule_draft_executes():
    res = tools.execute_tool(None, "schedule_draft",
                             {"text": "tool test post with a question?",
                              "when": "in 1 hour"})
    assert res["ok"], res
    did = res["draft_id"]
    d = db.get_draft(did)
    assert d["status"] == "approved"
    assert d["scheduled_at"] is not None
    assert d["meta"]["source"] == "chat-tool"
    assert d["meta"]["alg"]["score"] > 0
    db.update_draft(did, status="rejected")


def test_list_drafts_reports_real_ids():
    d1 = db.add_draft(text="draft one that waits in the approval queue")
    d2 = db.add_draft(text="draft two that also waits in the approval queue")
    res = tools.execute_tool(None, "list_drafts", {})
    assert res["ok"], res
    ids = [d["id"] for d in res["drafts"]]
    assert d1 in ids and d2 in ids
    previews = {d["id"]: d["text"] for d in res["drafts"]}
    assert "draft one that waits" in previews[d1]
    # status filter (newest approved first) + limit + junk status refusal
    db.update_draft(d1, status="approved")
    res = tools.execute_tool(None, "list_drafts", {"status": "approved", "limit": 1})
    assert res["ok"] and [d["id"] for d in res["drafts"]] == [d1]
    res = tools.execute_tool(None, "list_drafts", {"status": "nope"})
    assert res["ok"] is False and "status" in res["error"]
    db.update_draft(d1, status="rejected")
    db.update_draft(d2, status="rejected")


def test_schedule_draft_without_time_is_draft():
    res = tools.execute_tool(None, "schedule_draft", {"text": "no time given post"})
    assert res["ok"]
    d = db.get_draft(res["draft_id"])
    assert d["status"] == "draft" and d["scheduled_at"] is None
    db.update_draft(res["draft_id"], status="rejected")


def test_query_analytics_tool():
    db.upsert_post({"x_id": "tooltest-1", "author_handle": "u", "is_own": 1,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "text": "test post for analytics", "impressions": 1000,
                    "likes": 50, "reposts": 5, "replies": 3})
    res = tools.execute_tool(None, "query_analytics", {"timeframe": "all"})
    assert res["ok"], res
    assert res["posts"] >= 1
    assert "best_post" in res and "best_hours" in res


def test_pick_idea_tool():
    db.add_idea("tool test idea", "an angle", "one-liner", "test", 8.0)
    res = tools.execute_tool(None, "pick_idea", {})
    assert res["ok"], res
    assert res["top"]["title"]


def test_unknown_tool_and_bad_args():
    assert tools.execute_tool(None, "nope", {})["ok"] is False
    res = tools.execute_tool(None, "query_analytics", {"timeframe": "WRONG"})
    assert res["ok"] is False  # bad enum → error dict, not exception


def test_chat_candidates_extraction():
    from openstanley.core.config import Config
    reply = ("here you go:\n"
             "> building in public beats building in private. what are you building?\n\n"
             "want it scheduled?")
    cands = chat._extract_candidates(reply, Config())
    assert len(cands) == 1
    assert cands[0]["text"].startswith("building in public")
    assert cands[0]["alg"]["score"] > 0
    assert cands[0]["language"] == "en"


def test_chat_reply_with_actions_and_followup():
    """Fake LLM: first call returns an action block; follow-up reports results."""
    from openstanley.core.config import Config
    calls = {"n": 0}

    def fake_llm_chat(cfg, system, user, temperature=None, json_mode=False, retries=2):
        calls["n"] += 1
        if chat.FOLLOWUP_MARKER in system:
            return "queued — it's on the calendar."
        return (f"on it.\n{FENCE}action\n"
                '{"tool": "schedule_draft", "args": {"text": "fake tool post", "when": "in 2 hours"}}\n'
                f"{FENCE}")

    chat.llm_chat = fake_llm_chat
    result = chat.chat_reply(Config(), "schedule this for in 2 hours")
    assert calls["n"] >= 2, "follow-up turn must happen after tools"
    # agentic loop (2026-08-26): the follow-up's canned reply re-triggers
    # one more bounded round — the exact-2 pin predates the loop
    assert result["reply"].startswith("on it.")
    assert len(result["tool_results"]) == 1
    tr = result["tool_results"][0]
    assert tr["name"] == "schedule_draft" and tr["ok"]
    db.update_draft(tr["draft_id"], status="rejected")


def test_chat_stream_events():
    """Streaming path: token events, action event, done event — no network."""
    from openstanley.core.config import Config

    def fake_stream(cfg, system, user, temperature=None):
        for tok in ["hello", " world"]:
            yield tok

    chat.llm_chat_stream = fake_stream
    events = list(chat.chat_reply_stream(Config(), "hi"))
    types = [e["type"] for e in events]
    assert types.count("token") == 2
    assert "done" in types
    done = next(e for e in events if e["type"] == "done")
    assert done["reply_id"] > 0
    assert isinstance(done["actions"], list)


def test_parse_actions_accepts_flat_args_shape():
    """Models emit both {tool,args:{}} and flat {tool,topic:...} — the flat
    shape dropped every argument (user report: trend_post 'topic required')."""
    flat = '```action\n{"tool": "trend_post", "topic": "AI agents", "source": "x"}\n```'
    acts = tools.parse_actions(flat)
    assert acts and acts[0]["tool"] == "trend_post"
    assert acts[0]["args"].get("topic") == "AI agents"
    assert acts[0]["args"].get("source") == "x"
    nested = '```action\n{"tool": "trend_post", "args": {"topic": "crypto", "source": "web"}}\n```'
    acts2 = tools.parse_actions(nested)
    assert acts2[0]["args"] == {"topic": "crypto", "source": "web"}


def test_delete_draft_tool():
    """2026-08-29: the agent could list and schedule drafts but had NO way
    to remove one — 'Can u delete them now' got 'I still have no delete
    action'. delete_draft rejects (never hard-deletes) so history and the
    rejection learner keep their data."""
    from openstanley.core import db as _db
    did = _db.add_draft(text="delete me tool test draft", acct=1)
    res = tools.execute_tool(__import__("openstanley.core.config", fromlist=["Config"]).Config(),
                       "delete_draft", {"draft_id": did})
    assert res["ok"] and res["rejected"] == did
    assert _db.get_draft(did, acct=1)["status"] == "rejected"
    assert _db.get_draft(did, acct=1)["meta"].get("rejected_reason") == "owner"
    with _db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))


def test_delete_draft_bad_args_honest():
    res = tools.execute_tool(__import__("openstanley.core.config", fromlist=["Config"]).Config(),
                       "delete_draft", {})
    assert not res["ok"]
    res2 = tools.execute_tool(__import__("openstanley.core.config", fromlist=["Config"]).Config(),
                        "delete_draft", {"draft_id": 99999999})
    assert not res2["ok"]


def test_list_drafts_query_searches_all_statuses():
    """Live 2026-08-29: 'where is the seedance draft' got 'not among them,
    posted/deleted/never-saved' because the agent only pulled PENDING —
    while the draft sat APPROVED. query now searches all four statuses."""
    from openstanley.core import db as _db
    from openstanley.core.config import Config
    a = _db.add_draft(text="query probe alpha zebra unique", acct=1,
                      status="draft")
    b = _db.add_draft(text="query probe beta zebra unique", acct=1,
                      scheduled_at="2099-01-01T10:00:00", status="approved")
    res = tools.execute_tool(Config(), "list_drafts",
                             {"query": "zebra unique"})
    found = {d["id"] for d in res["drafts"]}
    assert a in found and b in found, res
    statuses = {d["id"]: d["status"] for d in res["drafts"]}
    assert statuses[b] == "approved"
    with _db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id IN (?,?)", (a, b))


def test_get_schedule_one_call_calendar():
    """Near-future probes (now+2min, now+4min) always land inside the
    next-12 slice regardless of ambient shared-queue depth — 2099 dates
    sort last and get capped out in a deep queue."""
    from datetime import datetime, timedelta
    from openstanley.core import db as _db
    from openstanley.core.config import Config
    t_early = (datetime.now() + timedelta(minutes=2)).isoformat(timespec="seconds")
    t_late = (datetime.now() + timedelta(minutes=4)).isoformat(timespec="seconds")
    late = _db.add_draft(text="schedule probe gamma zebra", acct=1,
                         scheduled_at=t_late, status="approved")
    early = _db.add_draft(text="schedule probe delta zebra", acct=1,
                          scheduled_at=t_early, status="approved")
    res = tools.execute_tool(Config(), "get_schedule", {})
    ids = [r["id"] for r in res["upcoming_first"]]
    assert early in ids and late in ids
    assert ids.index(early) <= ids.index(late), "time-ordered"
    with _db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id IN (?,?)", (early, late))


def test_list_drafts_previews_long_enough_to_read():
    """Self-contained: seeds its own long draft — ambient queue lengths
    are irrelevant to the preview contract."""
    from openstanley.core import db as _db
    from openstanley.core.config import Config
    long_text = ("self contained preview probe kino seedance studio "
                 "open source film studio director agent timeline editor "
                 "transitions captions export mp4 github repo link first "
                 "reply under the post end")
    did = _db.add_draft(text=long_text, acct=1, status="draft")
    res = tools.execute_tool(Config(), "list_drafts", {"limit": 10})
    mine = next(d for d in res["drafts"] if d["id"] == did)
    assert 120 < len(mine["text"]) <= 280, len(mine["text"])
    with _db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))
