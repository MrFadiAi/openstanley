"""Deep train — the full-immersion brain build for the ACTIVE account."""
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

    async def me(self):
        return {"username": "trainee", "followers": 42}

    async def user_tweets(self, handle, limit=100):
        return [{"x_id": f"tp{i}", "author_handle": handle, "is_own": 1,
                 "created_at": "2026-08-20T10:00:00",
                 "text": f"training post {i} about building in public",
                 "likes": i, "impressions": i * 30, "engagement": i}
                for i in range(min(limit, 60))]

    async def user_replies(self, handle, limit=100):
        return [{"x_id": f"tr{i}", "author_handle": handle, "is_own": 1,
                 "created_at": "2026-08-21T10:00:00",
                 "text": f"reply {i} in the wild", "likes": 1,
                 "impressions": 40, "engagement": 1}
                for i in range(min(limit, 20))]

    async def search(self, q, limit=50):
        return [{"x_id": f"tn{i}", "author_handle": "nicheperson", "is_own": 0,
                 "created_at": "2026-08-20T11:00:00", "text": f"niche {i}",
                 "likes": 5, "impressions": 500, "engagement": 6}
                for i in range(3)]


def test_deep_train_report_card(monkeypatch):
    import asyncio
    import openstanley.gen.agent as agent_mod

    async def _fake_replenish(*a, **k):
        return {"added": 0, "ran": False, "bank_before": 5}
    monkeypatch.setattr(agent_mod.ideas_mod, "replenish", _fake_replenish)

    import openstanley.gen.style_scan as scan_mod
    async def _fake_scan(cfg, x, max_posts=0, acct=None):
        return {"stats": {"posts_scanned": 80, "avg_length_chars": 90.0,
                          "language_mix": {"en": 0.9, "ar": 0.1},
                          "posting_times": {"best_hours": [9, 13]}},
                "human_summary": "trained"}
    monkeypatch.setattr(scan_mod, "scan_account", _fake_scan)

    import openstanley.gen.brain as brain_mod
    monkeypatch.setattr(brain_mod, "reflect",
                        lambda cfg, trigger, payload=None, acct=None: {
                            "ok": True,
                            "applied": {"added_rules": [{"id": 1, "text": "r"}],
                                        "retired_rules": [],
                                        "strategy_updates": [],
                                        "instructions_updated": True},
                            "journal_entry": "trained"})
    async def _fake_reflect(trigger, cfg, acct=None):
        return "brain: +1 rules"
    monkeypatch.setattr(agent_mod, "_reflect", _fake_reflect)

    cfg = Config()
    a = Agent(cfg)
    a.x = FakeX()
    report = asyncio.run(a.deep_train())
    assert report["handle"] == "trainee"
    assert report["posts_ingested"] > 0 and report["replies_ingested"] > 0
    assert "brain_rules" in report and "hooks" in report
    assert report["seconds"] >= 0
