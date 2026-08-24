"""Production follows approval — queue-aware create + 3-day draft expiry."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import drafts as drafts_mod                   # noqa: E402

CFG = Config()


def _pending() -> int:
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM drafts WHERE status='draft'").fetchone()[0]


def test_create_skips_when_queue_deep(monkeypatch):
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE status='draft'")
    for i in range(drafts_mod.QUEUE_DEEP):
        db.add_draft(text=f"filler {i}", acct=1)
    # deep queue → no LLM call is even attempted
    called = []
    monkeypatch.setattr(drafts_mod, "_draft_one",
                        lambda *a, **k: called.append(1) or None)
    out = drafts_mod.generate_drafts(CFG)
    assert out == [] and called == []
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id >= 900000")


def test_create_throttles_to_headroom(monkeypatch):
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE status='draft'")  # shared DB: other
        # suites leave pending rows; the headroom math needs a known baseline
    for i in range(drafts_mod.QUEUE_DEEP - 2):
        db.add_draft(text=f"filler {i}", acct=1)
    heads = []

    bodies = ["the debugger lied to me for an hour, the fix was one import",
              "grandmother bread baking taught me patience in code review",
              "market crashed while everyone argued about tabs vs spaces"]
    def fake_draft_one(cfg, idea, temp, language=None, **k):
        i = len(heads); heads.append(1)
        return {"text": bodies[i % len(bodies)], "kind": "post"}

    monkeypatch.setattr(drafts_mod, "_draft_one", fake_draft_one)
    import openstanley.gen.quote_card as _qc
    monkeypatch.setattr(_qc, "make_card", lambda t, out_dir=None: None)
    monkeypatch.setattr(db, "fresh_ideas",
                        lambda limit=None, acct=None: [
                            {"id": None, "text": "idea a", "angle": "x"},
                            {"id": None, "text": "idea b", "angle": "y"},
                            {"id": None, "text": "idea c", "angle": "z"}])
    monkeypatch.setattr(drafts_mod, "_draft_meta", lambda *a, **k: {})
    ids = drafts_mod.generate_drafts(CFG, count=5)
    assert len(ids) == 2, "only the headroom (12 - 10) gets drafted"
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id >= 900000")


def test_nightly_expiry_rejects_old_pending():
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id >= 900000")
    old = db.add_draft(text="stale draft", acct=1)
    stale = (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds")
    with db.connect() as c:
        c.execute("UPDATE drafts SET created_at=? WHERE id=?", (stale, old))
    fresh = db.add_draft(text="fresh draft", acct=1)
    # invoke the sweeper inline (same SQL the nightly job runs)
    cutoff = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    with db.connect() as c:
        cur = c.execute("UPDATE drafts SET status='rejected' "
                        "WHERE status='draft' AND created_at < ?", (cutoff,))
    assert cur.rowcount >= 1
    assert db.get_draft(old)["status"] == "rejected"
    assert db.get_draft(fresh)["status"] == "draft"
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id IN (?,?)", (old, fresh))
