"""Cross-account approvals must never be silent no-ships.

Live incident 2026-08-28: the owner approved 11 week-old account-1 TG cards
while account 2 was active — accepted, scheduled, and silently unpublishable
(the publish loop serves the ACTIVE account's X client only). The system now
(1) warns in the approve toast, (2) alerts from the publish loop, once per
draft.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.gen.agent import Agent                            # noqa: E402


def _ensure_accounts(*ids: int) -> None:
    """The shared test DB may lack the account rows (deletes by other
    suites) — set_active_account silently no-ops without them."""
    with db.connect() as c:
        for i in ids:
            c.execute("INSERT OR IGNORE INTO accounts (id, handle, status) "
                      "VALUES (?, ?, 'active')", (i, f"acct{i}"))


def _mk_due(acct: int, past_min: int = 60) -> int:
    due = (datetime.now() - timedelta(minutes=past_min)).isoformat(timespec="seconds")
    did = db.add_draft(text=f"stranded test {acct} {past_min}", kind="post",
                       acct=acct, scheduled_at=due, status="approved")
    return did


def test_publish_alerts_stranded_once(monkeypatch):
    _ensure_accounts(1, 2)
    db.set_active_account(2)
    db.set_setting("stranded_alerted", [])
    sent = []
    import openstanley.integrations.telegram as tg
    monkeypatch.setattr(tg, "is_enabled", lambda: True)
    monkeypatch.setattr(tg, "notify_bg",
                        lambda text: sent.append(text))
    a = Agent.__new__(Agent)  # no client build — we only call _alert_stranded
    did = _mk_due(1)
    a._alert_stranded(2)
    assert sent and f"#{did}" in sent[0] and "account 1" in sent[0]
    # second pass: same draft already alerted → silence (no re-alert spam)
    a._alert_stranded(2)
    assert len(sent) == 1
    # publish loop must never raise from any of this
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))
    db.set_setting("stranded_alerted", [])


def test_no_stranded_no_alert(monkeypatch):
    _ensure_accounts(1, 2)
    db.set_active_account(2)
    db.set_setting("stranded_alerted", [])
    sent = []
    import openstanley.integrations.telegram as tg
    monkeypatch.setattr(tg, "is_enabled", lambda: True)
    monkeypatch.setattr(tg, "notify_bg", lambda text: sent.append(text))
    a = Agent.__new__(Agent)
    a._alert_stranded(2)
    assert sent == []


def test_tg_approve_explains_cross_account(monkeypatch):
    """get_draft is active-account-filtered, so a cross-account approve
    fails with 'No draft #N' — the explainer must say it belongs to
    another account instead of sending the owner hunting."""
    from openstanley.integrations import telegram as tg_mod
    _ensure_accounts(1, 2)
    db.set_active_account(2)
    did = db.add_draft(text="old orbexai draft", kind="post", acct=1,
                       status="draft")
    msg = tg_mod.approve_draft_tg(None, did)
    assert "No approvable draft" in msg and "ACCOUNT 1" in msg and "/account 1" in msg, msg
    msg_r = tg_mod.reject_draft_tg(did)
    assert "ACCOUNT 1" in msg_r, msg_r
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))
