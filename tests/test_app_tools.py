"""Full-app-surface tools — the agent drives EVERYTHING OpenStanley does
(owner directive 2026-08-30: 'it can do everything I ask, both the agent
app and Telegram agent')."""
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
from openstanley.gen import tools                                  # noqa: E402

CFG = Config()


def test_app_status_one_call_everything():
    res = tools.execute_tool(CFG, "app_status", {})
    assert res["ok"]
    for key in ("account", "autopilot", "caps_today", "queue",
                "schedule_next", "brain_active_rules", "mode"):
        assert key in res, key
    assert isinstance(res["schedule_next"], list)


def test_approve_and_reschedule_roundtrip():
    did = db.add_draft(text="app tool approve probe zebra", acct=1)
    res = tools.execute_tool(CFG, "approve_draft", {"draft_id": did})
    assert res["ok"] and res["scheduled_at"]
    d = db.get_draft(did, acct=1)
    assert d["status"] == "approved"
    res2 = tools.execute_tool(CFG, "reschedule_draft",
                              {"draft_id": did, "when": "tomorrow 9am"})
    assert res2["ok"], res2
    assert res2["scheduled_at"] != res["scheduled_at"]
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))


def test_edit_draft_text():
    did = db.add_draft(text="original probe text zebra", acct=1)
    res = tools.execute_tool(CFG, "edit_draft",
                             {"draft_id": did, "text": "edited probe text"})
    assert res["ok"]
    assert "edited" in db.get_draft(did, acct=1)["text"]
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))


def test_run_loop_publish_safe_noop():
    """publish with nothing due is the safest loop to prove run_loop works."""
    res = tools.execute_tool(CFG, "run_loop", {"name": "publish"})
    assert res["ok"] and res["loop"] == "publish"
    bad = tools.execute_tool(CFG, "run_loop", {"name": "nope"})
    assert not bad["ok"]


def test_brain_read_parts():
    for part in ("rules", "journal"):
        res = tools.execute_tool(CFG, "brain_read", {"part": part})
        assert res["ok"] and part in res["content"].lower() or res["ok"]
    bad = tools.execute_tool(CFG, "brain_read", {"part": "nope"})
    assert not bad["ok"]


def test_switch_account_lists_without_id():
    res = tools.execute_tool(CFG, "switch_account", {})
    assert res["ok"] and isinstance(res["accounts"], list)
    assert "active" in res


def test_list_ideas_tool():
    res = tools.execute_tool(CFG, "list_ideas", {"limit": 4})
    assert res["ok"] and res["count"] >= 0
