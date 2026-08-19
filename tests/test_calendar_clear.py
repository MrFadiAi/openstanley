"""Calendar clear-all buttons — bulk delete scheduled / queued drafts.

Safety pins: published history and rejected drafts are NEVER deleted, no
matter which clear button fires.
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

from fastapi.testclient import TestClient                          # noqa: E402
from openstanley.server.__main__ import app                        # noqa: E402


def _seed():
    """Fresh mix: 2 scheduled approved, 1 scheduled pending (draft status),
    2 queued, 1 published, 1 rejected."""
    with db.connect() as c:
        c.execute("DELETE FROM drafts")
    ids = {}
    for name, text in [("sch1", "scheduled one"), ("sch2", "scheduled two")]:
        ids[name] = db.add_draft(text=text, acct=1)
        db.update_draft(ids[name], acct=1, status="approved",
                        scheduled_at="2099-01-01T09:00:00")
    ids["pend"] = db.add_draft(text="pending scheduled reply", kind="reply", acct=1)
    db.update_draft(ids["pend"], acct=1, status="draft",
                    scheduled_at="2099-01-02T09:00:00")
    ids["q1"] = db.add_draft(text="queued one", acct=1)
    ids["q2"] = db.add_draft(text="queued two", acct=1)
    ids["pub"] = db.add_draft(text="already out", acct=1)
    db.update_draft(ids["pub"], acct=1, status="published",
                    published_at="2026-08-19T09:00:00", x_id="1")
    ids["rej"] = db.add_draft(text="rejected stays", acct=1)
    db.update_draft(ids["rej"], acct=1, status="rejected")
    return ids


def _existing(ids: dict) -> set[int]:
    with db.connect() as c:
        return {r[0] for r in c.execute("SELECT id FROM drafts")} & set(ids.values())


def test_clear_scheduled_deletes_placed_only():
    ids = _seed()
    with TestClient(app) as client:
        r = client.post("/api/drafts/clear-scheduled")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 3           # 2 approved + 1 pending
    left = _existing(ids)
    assert left == {ids["q1"], ids["q2"], ids["pub"], ids["rej"]}


def test_clear_queue_deletes_unplaced_only():
    ids = _seed()
    with TestClient(app) as client:
        r = client.post("/api/drafts/clear-queue")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 2           # only the two unscheduled
    left = _existing(ids)
    assert left == {ids["sch1"], ids["sch2"], ids["pend"], ids["pub"], ids["rej"]}


def test_clear_endpoints_never_touch_published_or_rejected():
    ids = _seed()
    with TestClient(app) as client:
        client.post("/api/drafts/clear-scheduled")
        client.post("/api/drafts/clear-queue")
    left = _existing(ids)
    assert left == {ids["pub"], ids["rej"]}


def test_clear_on_empty_schedule_is_zero_not_error():
    with db.connect() as c:
        c.execute("DELETE FROM drafts")
    with TestClient(app) as client:
        r = client.post("/api/drafts/clear-scheduled")
    assert r.status_code == 200 and r.json()["deleted"] == 0
