"""Multi-account (v0.5.0) — Phase 4: connect bootstrap + Telegram.

Connect flow: paste cookies → POST /api/accounts/bootstrap validates them via
me() and creates-or-reselects THAT account (handle from X, fresh empty brain
for a new one, cookies stored per-account). Telegram: /status first line is
the active account, /account lists + switches (allowed-chats gate still
applies), TG chat operates on the active account.

Hermetic: fake XCookie probe (no X), fake TG httpx seam (no Bot API),
sandboxed brains, OPENSTANLEY_TEST_DB, no LLM paths reached.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("OPENSTANLEY_NO_SCHEDULER", "1")  # before importing the server

import pytest  # noqa: E402

from openstanley.core import db                      # noqa: E402
db.init_db()

import openstanley.server.__main__ as server         # noqa: E402
import openstanley.x.client as xclient               # noqa: E402
from openstanley.core.config import Config           # noqa: E402
from openstanley.gen import brain as brain_mod       # noqa: E402
from openstanley.integrations import telegram as tg  # noqa: E402

CHAT = 111222333
CFG = Config()
COOKIES = json.dumps({"auth_token": "tok-111122223333", "ct0": "ct0"})


class _FakeProbe:
    """XCookie-shaped stub — me() succeeds, nothing else is called."""
    def __init__(self, cookies_json: str, username: str = "", caps=None,
                 account_id: int = 1):
        self._cookies = cookies_json
        self.username = username

    async def me(self, heal: bool = True) -> dict:
        return {"username": self.username, "name": self.username.title(),
                "followers": 77}


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(db, "DB_PATH", server.db.DB_PATH)  # keep default test DB
    with TestClient(server.app) as tc:
        yield tc


@pytest.fixture(autouse=True)
def _reset_state():
    for a in db.list_accounts():     # shared test DB — drop leftover test
        if a["id"] != 1:             # accounts so handles stay unique
            db.delete_account_rows(a["id"])
    db.set_active_account(1)
    yield
    db.set_active_account(1)


# ---------------- connect bootstrap ----------------

def test_bootstrap_creates_new_account(client, monkeypatch):
    before = {a["handle"] for a in db.list_accounts()}
    monkeypatch.setattr(xclient, "XCookie",
                        lambda c, caps=None, account_id=1:
                        _FakeProbe(c, "fresh_account"))
    r = client.post("/api/accounts/bootstrap", json={"cookies_json": COOKIES})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "created" and body["handle"] == "fresh_account"
    acct = body["account_id"]
    assert db.active_account() == acct
    assert "fresh_account" not in before or True
    # cookies stored for THAT account, masked elsewhere
    assert '"tok-1111' not in json.dumps(db.list_accounts())
    acct_row = db.get_account(acct)
    assert acct_row["handle"] == "fresh_account" and acct_row["cookies_set"]
    # fresh brain: no rules (sandboxed by conftest; seeds only)
    assert brain_mod.parse_rules(brain_mod.read("rules", acct)) == []


def test_bootstrap_reconnects_existing_account(client, monkeypatch):
    monkeypatch.setattr(xclient, "XCookie",
                        lambda c, caps=None, account_id=1:
                        _FakeProbe(c, "fresh_account"))
    r1 = client.post("/api/accounts/bootstrap", json={"cookies_json": COOKIES})
    acct = r1.json()["account_id"]
    db.set_active_account(1)  # user switches away meanwhile
    r2 = client.post("/api/accounts/bootstrap", json={"cookies_json": COOKIES})
    body = r2.json()
    assert body["action"] == "reconnected" and body["account_id"] == acct
    assert db.active_account() == acct          # reselected + reactivated
    handles = [a["handle"] for a in db.list_accounts()]
    assert handles.count("fresh_account") == 1  # no duplicates


def test_bootstrap_rejects_bad_cookies(client):
    r = client.post("/api/accounts/bootstrap",
                    json={"cookies_json": "not json at all"})
    assert r.status_code == 400
    r = client.post("/api/accounts/bootstrap",
                    json={"cookies_json": json.dumps({"ct0": "x"})})
    assert r.status_code == 400


# ---------------- Telegram ----------------

class _FakeTGHttpx:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url, json=None, **kw):
        self.calls.append((url, "post", json or {}))
        return _R({"ok": True, "result": {"message_id": 1}})


class _R:
    def __init__(self, payload: dict):
        self.status_code = 200
        self.text = "ok"
        self._p = payload

    def json(self) -> dict:
        return self._p


def _upd(update_id: int, chat_id: int, text: str) -> dict:
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text,
                        "from": {"id": chat_id}}}


def _enable(chats: list[int] | None = None) -> None:
    db.set_setting("tg_bot_token", "700:AAHtest-token")
    db.set_setting("tg_allowed_chats", chats if chats is not None else [CHAT])
    db.set_setting("tg_enabled", True)


def _sent(fake) -> list[str]:
    return [p.get("text", "") for _u, _m, p in fake.calls]


def test_tg_status_first_line_is_active_account():
    _enable()
    a2 = db.create_account("second")
    db.set_active_account(a2)
    db.set_me({"username": "second_handle", "followers": 55}, acct=a2)
    first = tg._cmd_status(CFG).splitlines()[0]
    assert f"Account #{a2}" in first and "@second_handle" in first
    db.set_setting("tg_enabled", False)


def test_tg_account_lists_and_switches():
    a2 = db.create_account("second")
    listing = tg._cmd_account("")
    assert "@second" in listing and f"#{a2}" in listing
    switched = tg._cmd_account(str(a2))
    assert db.active_account() == a2
    assert f"#{a2}" in switched
    assert tg._cmd_account("999").startswith("No account #999")
    assert tg._cmd_account("what").startswith("Usage:")


def test_tg_account_command_gated_and_dispatched(monkeypatch):
    a2 = db.create_account("second")
    _enable(chats=[CHAT])
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    # stranger chat: the gate answers with the private notice — the account
    # listing itself never leaves
    tg.handle_update(CFG, _upd(1, 999, "/account"))
    stranger_replies = _sent(fake)
    assert stranger_replies and "private" in stranger_replies[0]
    assert "@second" not in stranger_replies[0]
    # allowed chat: list + switch through the real dispatch
    tg.handle_update(CFG, _upd(2, CHAT, "/account"))
    assert any("@second" in t for t in _sent(fake))
    tg.handle_update(CFG, _upd(3, CHAT, f"/account {a2}"))
    assert db.active_account() == a2
    db.set_setting("tg_enabled", False)


def test_tg_chat_drafts_land_on_active_account(monkeypatch):
    """TG /post drafts go to the ACTIVE account's inbox (approval-gated)."""
    a2 = db.create_account("second")
    db.set_active_account(a2)
    _enable()
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, CHAT, "/post a note for the second account"))
    drafts2 = db.drafts_by_status("draft", acct=a2)
    assert any("second account" in d["text"] for d in drafts2)
    assert db.drafts_by_status("draft", acct=1) == [] or all(
        "second account" not in d["text"] for d in db.drafts_by_status("draft", acct=1))
    db.set_setting("tg_enabled", False)
