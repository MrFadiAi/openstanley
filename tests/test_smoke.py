"""Smoke test: dry-run pipeline without LLM or X — validates DB + agent plumbing."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openstanley.core import db                      # noqa: E402
db.init_db()

from openstanley.core.config import Config           # noqa: E402
from openstanley.x.client import XDry                # noqa: E402


def main() -> int:
    db.init_db()
    cfg = Config()
    dry = XDry("test_user")

    # 1. simulated import
    own = asyncio.run(dry.user_tweets("test_user", 50))
    assert len(own) == 50, f"expected 50 own tweets, got {len(own)}"
    for p in own:
        db.upsert_post(p)
    niche = asyncio.run(dry.search("AI agents", 30))
    assert len(niche) == 30
    for p in niche:
        db.upsert_post(p)
    stats = db.dashboard_stats()
    assert stats["own_posts"] >= 50 and stats["niche_posts"] >= 30, stats
    print(f"[ok] import: own={stats['own_posts']} niche={stats['niche_posts']}")

    # 2. ideas + drafts plumbing (no LLM — direct DB ops)
    iid = db.add_idea("test idea", "test angle", "one-liner", "test", 7.5)
    did = db.add_draft(text="test draft text", idea_id=iid,
                       meta={"idea_title": "test idea"})
    assert db.idea_count() >= 1
    d = db.drafts_by_status("draft", 10)[0]
    assert d["id"] == did
    print(f"[ok] ideas+drafts: idea={iid} draft={did}")

    # 3. approve → queue → publish (dry)
    db.update_draft(did, status="approved", scheduled_at="2000-01-01T00:00:00")
    nxt = db.next_scheduled()
    assert nxt and nxt["id"] == did
    res = asyncio.run(dry.post_tweet(nxt["text"]))
    assert res["dry_run"] is True
    db.update_draft(did, status="published", x_id=res["x_id"])
    print(f"[ok] publish dry-run: {res}")

    # 4. voice profile roundtrip
    db.save_voice('{"tone":"dry"}', [{"text": "example", "likes": 5}])
    v = db.load_voice()
    assert v["examples"][0]["text"] == "example"
    print("[ok] voice profile roundtrip")

    print("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
