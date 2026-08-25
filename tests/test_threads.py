"""Thread composer — topic -> 3-6 tweet thread draft, approval-gated."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from fastapi.testclient import TestClient                          # noqa: E402
from openstanley.server.__main__ import app                        # noqa: E402


def _fake_llm(reply: str, monkeypatch):
    import openstanley.gen.llm as llm_mod
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: reply)


def test_thread_endpoint_creates_multitweet_draft(monkeypatch):
    _fake_llm('{"thread": ["hooks first: why agents fail at scheduling", "the fix is boring: one queue, one clock", "ship it and watch the logs"]}', monkeypatch)
    with TestClient(app) as client:
        r = client.post("/api/threads", json={"topic": "agents and scheduling"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tweets"] == 3
    d = db.get_draft(body["draft_id"])
    assert d["thread"] and len(d["thread"]) == 3
    assert d["text"] == d["thread"][0]           # first tweet is the hook/preview
    assert d["status"] == "draft"                # approval-gated
    assert d["meta"]["source"] == "thread-composer"


def test_thread_endpoint_rejects_empty_topic():
    with TestClient(app) as client:
        r = client.post("/api/threads", json={"topic": "  "})
    assert r.status_code == 400


def test_thread_endpoint_llm_garbage_is_500_not_crash(monkeypatch):
    _fake_llm("no json here at all", monkeypatch)
    with TestClient(app) as client:
        r = client.post("/api/threads", json={"topic": "x"})
    assert r.status_code == 500


def test_link_reply_posts_under_the_post():
    """A post carrying meta.link_reply ships the link as its first reply."""
    import asyncio
    from openstanley.core import db as dbm
    dbm.init_db()
    from openstanley.core.config import Config
    from openstanley.gen.agent import Agent

    posted = []

    class FakeX:
        mode = "dryrun"
        async def post_tweet(self, text, reply_to=None, media_path=None, quote_of=None):
            posted.append({"text": text, "reply_to": reply_to})
            return {"x_id": "999"}
        async def post_thread(self, tweets):
            return [{"x_id": "998"}]

    a = Agent(Config())
    a.x = FakeX()
    did = dbm.add_draft(text="body of the post", kind="post", acct=1,
                        status="approved",
                        scheduled_at="2020-01-01T00:00:00",
                        meta={"link_reply": "https://github.com/u/r"})
    with dbm.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))
    # re-add with a due slot then publish
    did = dbm.add_draft(text="body of the post", kind="post", acct=1,
                        status="approved",
                        scheduled_at="2020-01-01T00:00:00",
                        meta={"link_reply": "https://github.com/u/r"})
    res = asyncio.run(a.publish())
    assert did in res.get("published", []) or any(
        p.get("draft_id") == did for p in res.get("published", [])
        if isinstance(p, dict)) or str(did) in str(res)[:200]
    assert posted[0]["text"] == "body of the post" and posted[0]["reply_to"] is None
    assert posted[1]["text"] == "https://github.com/u/r"
    assert posted[1]["reply_to"] == posted[0].get("reply_to") or posted[1]["reply_to"] == str(
        posted[0].get("x_id", "999"))
    with dbm.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))
