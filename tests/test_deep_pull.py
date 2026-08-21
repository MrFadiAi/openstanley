"""New accounts learn their real history, not a 30-post search sample."""
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
    mode = "dryrun"

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
        # pretend the account has 230 posts: 100+100+30 across pages
        remaining = {100: 100, 200: 100, 300: 100, 400: 30}
        n = 0
        for called, cnt in reversed(self.tweets_calls):
            if called == handle:
                n += cnt
                break
        total_before = sum(c for h, c in self.tweets_calls if h == handle) - limit
        made = max(0, 230 - total_before)
        return [{"x_id": f"t{total_before + i}", "author_handle": handle,
                 "is_own": 1, "text": f"real post {total_before + i}",
                 "likes": 5, "impressions": 100} for i in range(min(made, limit))]


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
    assert n >= 200, f"deep pull should page the full history, got {n}"
    assert any(h == "newacct" for h, _l in fx.tweets_calls), fx.tweets_calls
    assert len(fx.tweets_calls) >= 3, "must page past the first 100"
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE x_id LIKE 't%' OR x_id LIKE 's%'")
