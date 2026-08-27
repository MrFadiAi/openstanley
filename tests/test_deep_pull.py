"""New accounts learn their real history, not a 30-post search sample.

2026-08-28 regression pin: the deep pull used CHUNKED user_tweets calls —
but the cookie client pages via cursor WITHIN one call, so every chunk
restarted at page 1. Production logged "+400 posts" daily while storing
nothing (own count stuck at ~104 for days). The fake here models the REAL
client: one call with a big limit returns the whole deep history.
"""
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

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen.agent import Agent                            # noqa: E402


class FakeX:
    """Models the REAL cookie client: user_tweets(limit=N) pages via cursor
    internally and returns up to N posts in ONE call. Separate calls
    restart from the top of the timeline (this is what broke the chunked
    deep pull)."""
    mode = "dryrun"
    HISTORY = 230

    def __init__(self):
        self.tweets_calls = []

    async def me(self):
        return {"username": "newacct", "name": "n", "followers": 10}

    async def search(self, q, limit=50):
        return [{"x_id": f"s{i}", "author_handle": "someone", "is_own": 0,
                 "text": f"search hit {i}", "likes": i, "impressions": i * 20}
                for i in range(min(limit, 3))]

    async def user_tweets(self, handle, limit=100):
        self.tweets_calls.append((handle, limit))
        n = min(limit, self.HISTORY)
        return [{"x_id": f"t{i}", "author_handle": handle,
                 "is_own": 1, "text": f"real post {i}",
                 "likes": 5, "impressions": 100} for i in range(n)]


def test_study_deep_pulls_new_account_history(monkeypatch):
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE x_id LIKE 't%' OR x_id LIKE 's%'")
    fx = FakeX()
    cfg = Config()
    a = Agent(cfg)
    a.x = fx
    import inspect
    import openstanley.gen.agent as agent_mod

    async def _fake_replenish(*ar, **kw):
        return {"added": 0}

    assert inspect.iscoroutinefunction(_fake_replenish)
    monkeypatch.setattr(agent_mod.ideas_mod, "replenish", _fake_replenish)
    import asyncio
    db.set_me({"username": "newacct", "followers": 10})
    res = asyncio.run(a.study())
    with db.connect() as c:
        (n,) = c.execute(
            "SELECT COUNT(*) FROM posts WHERE x_id LIKE 't%'").fetchone()
    assert n >= 200, f"deep pull should fetch the full history, got {n}"
    deep_calls = [limit for h, limit in fx.tweets_calls if h == "newacct"]
    assert deep_calls and max(deep_calls) >= 200, (
        f"deep pull must ask for the history in ONE big internally-paged "
        f"call, got {fx.tweets_calls}")
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE x_id LIKE 't%' OR x_id LIKE 's%'")


def test_second_study_run_pulls_nothing_new(monkeypatch):
    """With >=150 stored, the deep pull is skipped entirely — and when it
    does run, the log reports NET NEW stored rows, never fetch counts."""
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE x_id LIKE 't%' OR x_id LIKE 's%'")
    fx = FakeX()
    a = Agent(Config())
    a.x = fx
    import asyncio
    import openstanley.gen.agent as agent_mod

    async def _fake_replenish(*ar, **kw):
        return {"added": 0}
    monkeypatch.setattr(agent_mod.ideas_mod, "replenish", _fake_replenish)
    db.set_me({"username": "newacct", "followers": 10})
    asyncio.run(a.study())
    # history now full (230 >= 150) → second run must not call user_tweets
    fx.tweets_calls.clear()
    asyncio.run(a.study())
    assert not [c for c in fx.tweets_calls if c[0] == "newacct"], \
        "deep pull must skip accounts it already knows"
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE x_id LIKE 't%' OR x_id LIKE 's%'")
