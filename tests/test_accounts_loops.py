"""Multi-account (v0.5.0) — loops, autopilot, X client per account (Phase 3).

Every loop pins the ACTIVE account at start (a mid-run switch can never mix
two accounts' data), logs which account it ran for, and returns it. Autopilot
drives the active account only (one at a time — simultaneous multi-account
autopilot is out of scope for v0.5). The cookie client resolves cookies per
account id: DB row wins, .env is a bootstrap fallback for account 1 ONLY.

Hermetic: dryrun X (or XCookie constructed but never connected), LLM seams
patched where a loop reaches them, sandboxed brains + test DB.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.setdefault("OPENSTANLEY_NO_SCHEDULER", "1")

import pytest  # noqa: E402

from openstanley.core import db                  # noqa: E402
db.init_db()

from openstanley.core.config import Config       # noqa: E402
from openstanley.gen import agent as agent_mod   # noqa: E402
from openstanley.gen import autopilot as ap      # noqa: E402
from openstanley.gen import ideas as ideas_mod   # noqa: E402
from openstanley.x import client as xclient      # noqa: E402

CFG = Config()

DRAFT_JSON = '{"tweet": "fresh account voice, zero leaks"}'


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_state():
    db.set_active_account(1)
    yield
    db.set_active_account(1)


# ---------------- loops pin the active account ----------------

def test_study_loop_writes_only_the_active_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "loops.db")
    db.init_db()
    a2 = db.create_account("second")
    from openstanley.gen import brain as brain_mod
    monkeypatch.setattr(brain_mod, "ACCOUNTS_ROOT", tmp_path / "accounts")
    brain_mod.ensure(1)
    brain_mod.ensure(a2)

    agent = agent_mod.Agent(CFG)  # dryrun X
    db.set_active_account(a2)
    res = _run(agent.study())

    assert res["account"] == a2
    assert res["bank"] == db.idea_count(acct=a2)
    assert db.idea_count(acct=1) == 0            # nothing leaked into account 1
    with db.connect() as c:
        (log_n,) = c.execute(
            "SELECT COUNT(*) FROM agent_log WHERE loop='study' "
            "AND message LIKE ?", (f"%[account {a2}]%",)).fetchone()
    assert log_n >= 1, "the loop logs which account it ran for"


def test_create_loop_drafts_into_active_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "create.db")
    db.init_db()
    a2 = db.create_account("second")
    from openstanley.gen import brain as brain_mod
    from openstanley.gen import drafts as drafts_mod
    monkeypatch.setattr(brain_mod, "ACCOUNTS_ROOT", tmp_path / "accounts")
    brain_mod.ensure(a2)
    db.add_idea("only for two", "angle", "one-liner", "scan", 9.0, acct=a2)
    monkeypatch.setattr(drafts_mod, "chat",
                        lambda *a, **k: DRAFT_JSON)  # hermetic LLM seam

    agent = agent_mod.Agent(CFG)
    db.set_active_account(a2)
    res = _run(agent.create())

    assert res["account"] == a2
    drafts2 = db.drafts_by_status("draft", acct=a2)
    assert drafts2, "at least the seeded idea got drafted"
    assert all("zero leaks" in d["text"] for d in drafts2)
    assert db.drafts_by_status("draft", acct=1) == []


def test_import_loop_pins_account_across_its_run(tmp_path, monkeypatch):
    """import_history reads the account ONCE; a switch mid-run must not mix
    data (simulate by switching during the client call)."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "import.db")
    db.init_db()
    a2 = db.create_account("second")
    agent = agent_mod.Agent(CFG)
    db.set_active_account(a2)

    original = agent.x.user_tweets

    async def switch_mid_run(username, limit=100):
        db.set_active_account(1)  # user flips accounts while the loop runs
        return await original(username, limit)

    monkeypatch.setattr(agent.x, "user_tweets", switch_mid_run)
    res = _run(agent.import_history())

    assert res["account"] == a2
    assert db.own_posts(acct=a2), "posts land in the account the loop started for"
    assert db.own_posts(acct=1) == []
    db.set_active_account(1)  # teardown: the mid-run switch left account 1 active


# ---------------- autopilot drives the ACTIVE account ----------------

def test_autopilot_tick_reports_and_logs_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ap.db")
    db.init_db()
    a2 = db.create_account("second")
    db.set_active_account(a2)
    ap.set_state(ticks=0, enabled=True, errors=[])

    class StubAgent:
        async def study(self):
            return {"ok": True}

    res = _run(ap.run_tick(StubAgent(), CFG))
    assert res["ok"] is True and res["account"] == a2
    with db.connect() as c:
        row = c.execute("SELECT message FROM agent_log WHERE loop='autopilot' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    assert f"[account {a2}]" in row["message"]
    ap.set_state(enabled=False)


def test_autopilot_autoapprove_scopes_to_active_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ap2.db")
    db.init_db()
    a2 = db.create_account("second")
    cfg = Config()
    cfg.agent.auto_approve_replies = True
    db.set_active_account(a2)
    ap.set_state(ticks=1, enabled=True, errors=[])  # phase 'create'? no —
    # phase = PHASES[1] = 'create'; we want engage → ticks=2
    ap.set_state(ticks=2, enabled=True, errors=[])

    d1 = db.add_draft(text="acct1 reply", kind="reply",
                      scheduled_at="2020-01-01T00:00:00", acct=1)

    class StubAgent:
        async def engage(self):
            # what the real loop does: create this run's reply drafts
            return {"drafts": [db.add_draft(text="acct2 reply", kind="reply",
                                            scheduled_at="2020-01-01T00:00:00",
                                            acct=a2)]}

    res = _run(ap._phase_engage(StubAgent(), cfg))
    assert res["approved_replies"] == 1
    d2 = db.drafts_by_status("approved", acct=a2)
    assert len(d2) == 1 and d2[0]["text"] == "acct2 reply"
    assert db.get_draft(d1, acct=1)["status"] == "draft"  # other account untouched
    ap.set_state(enabled=False)


# ---------------- X client: cookies per account ----------------

def test_build_client_resolves_cookies_per_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "xc.db")
    db.init_db()
    env_cookies = '{"auth_token": "env-token-1", "ct0": "e"}'
    monkeypatch.setenv(CFG.x.cookies_env, env_cookies)
    cfg = Config()
    cfg.x.mode = "cookie"

    a2 = db.create_account("second", '{"auth_token": "db-token-2", "ct0": "d"}')

    c1 = xclient.build_client(cfg, 1)
    assert c1.account_id == 1 and '"env-token-1"' in c1._cookies  # .env bootstrap
    c2 = xclient.build_client(cfg, 2)
    assert c2.account_id == 2 and '"db-token-2"' in c2._cookies   # DB wins
    c3 = xclient.build_client(cfg, 3)                              # no env fallback
    assert c3.account_id == 3 and c3._cookies == "{}"

    # DB wins over env even for account 1
    db.set_account_cookies(1, '{"auth_token": "db-token-1", "ct0": "d"}')
    assert '"db-token-1"' in xclient.build_client(cfg, 1)._cookies

    monkeypatch.delenv(CFG.x.cookies_env)
    # env cleared: account 2 still resolves from its DB row
    assert '"db-token-2"' in xclient.build_client(cfg, 2)._cookies
