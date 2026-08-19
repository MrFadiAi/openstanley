"""Bare-token cookie paste — one normalizer, every surface.

FIX_BRIEF_BARE_TOKEN: users paste whatever they have — a bare auth_token, a
browser cookie header ('auth_token=…; ct0=…'), or full JSON. The server
normalizes via x/client.normalize_cookies_input; the user never writes JSON.
Surfaces covered: POST /api/accounts (create), POST /api/accounts/{id}/cookies,
POST /api/accounts/bootstrap, POST /api/x/cookie-connect, and the .env
OPENSTANLEY_X_COOKIES fallback through resolve_cookies.

Hermetic: fake XCookie probe (no X), OPENSTANLEY_TEST_DB, sandboxed brains,
no LLM paths reached.
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
from openstanley.x.client import normalize_cookies_input  # noqa: E402

TOKEN = "db229a145c0d5ca3f7b6e89a0d1c2b3a4f5e6d7f"  # 40-hex, X-shaped
CANON = json.dumps({"auth_token": TOKEN}, separators=(",", ":"))


class _FakeProbe:
    """XCookie-shaped stub — me() succeeds, nothing else is called."""
    def __init__(self, cookies_json: str, username: str = "", caps=None,
                 account_id: int = 1):
        self._cookies = cookies_json
        self.username = username

    async def me(self) -> dict:
        return {"username": self.username or "probe_user",
                "name": "Probe User", "followers": 9}


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(db, "DB_PATH", server.db.DB_PATH)  # keep default test DB
    with TestClient(server.app) as tc:
        yield tc


@pytest.fixture(autouse=True)
def _reset_state():
    def _clean():
        for a in db.list_accounts():  # shared test DB — drop leftover test
            if a["id"] != 1:          # accounts so handles stay unique
                db.delete_account_rows(a["id"])
        db.set_account_handle(1, "")  # cookie-connect writes the active handle
        db.set_active_account(1)
    _clean()
    yield
    _clean()


# ---------------- normalizer unit tests ----------------

def test_bare_token_wraps_into_json():
    assert normalize_cookies_input(TOKEN) == CANON
    # whitespace + stray clipboard quotes tolerated
    assert normalize_cookies_input(f'  "{TOKEN}"  \n') == CANON
    assert normalize_cookies_input(f"'{TOKEN}'") == CANON


def test_cookie_header_any_separator():
    for raw in (f"auth_token={TOKEN}; ct0=abc123",
                f"auth_token={TOKEN};ct0=abc123",
                f"auth_token={TOKEN}\nct0=abc123",
                f"auth_token={TOKEN} ct0=abc123"):
        assert json.loads(normalize_cookies_input(raw)) == \
            {"auth_token": TOKEN, "ct0": "abc123"}, raw
    # quoted header values + extra cookies survive
    got = json.loads(normalize_cookies_input(
        f'auth_token="{TOKEN}"; ct0=\'abc\'; twid=u1'))
    assert got == {"auth_token": TOKEN, "ct0": "abc", "twid": "u1"}


def test_full_json_pass_through():
    raw = json.dumps({"auth_token": TOKEN, "ct0": "x", "twid": "u1"})
    got = json.loads(normalize_cookies_input(raw))
    assert got == {"auth_token": TOKEN, "ct0": "x", "twid": "u1"}
    # already-canonical input round-trips byte-identical
    assert normalize_cookies_input(
        json.dumps({"auth_token": TOKEN}, separators=(",", ":"))) == CANON


def test_garbage_yields_none():
    for bad in ("garbage", "", "   ", "\n", "not json at all", "{}",
                json.dumps({"ct0": "x"}),           # JSON without auth_token
                f"ct0=abc; twid=u1",                # header without auth_token
                "auth_token=",                      # empty value
                json.dumps({"auth_token": "  "})):  # whitespace-only value
        assert normalize_cookies_input(bad) is None, bad


# ---------------- endpoints: same normalizer everywhere ----------------

def test_bootstrap_accepts_bare_token(client, monkeypatch):
    monkeypatch.setattr(xclient, "XCookie",
                        lambda c, caps=None, account_id=1: _FakeProbe(c))
    r = client.post("/api/accounts/bootstrap", json={"cookies_json": TOKEN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "created" and body["handle"] == "probe_user"
    # canonical JSON stored — not the bare string
    assert db.account_cookies(body["account_id"]) == CANON


def test_bootstrap_accepts_cookie_header(client, monkeypatch):
    monkeypatch.setattr(xclient, "XCookie",
                        lambda c, caps=None, account_id=1: _FakeProbe(c))
    r = client.post("/api/accounts/bootstrap",
                    json={"cookies_json": f"auth_token={TOKEN}; ct0=abc"})
    assert r.status_code == 200, r.text
    stored = json.loads(db.account_cookies(r.json()["account_id"]))
    assert stored == {"auth_token": TOKEN, "ct0": "abc"}


def test_bootstrap_helpful_400_on_garbage(client):
    r = client.post("/api/accounts/bootstrap", json={"cookies_json": "garbage"})
    assert r.status_code == 400
    assert "auth_token" in r.json()["detail"]


def test_set_account_cookies_accepts_bare_token(client):
    r = client.post("/api/accounts/1/cookies", json={"cookies_json": TOKEN})
    assert r.status_code == 200, r.text
    assert db.account_cookies(1) == CANON
    # masked hint reflects the wrapped auth_token
    assert r.json()["cookies_masked"] == f"••••{TOKEN[-4:]}"


def test_set_account_cookies_accepts_header_and_quotes(client):
    r = client.post("/api/accounts/1/cookies",
                    json={"cookies_json": f'  "auth_token={TOKEN}; ct0=abc"  '})
    assert r.status_code == 200, r.text
    assert json.loads(db.account_cookies(1)) == {"auth_token": TOKEN, "ct0": "abc"}


def test_set_account_cookies_helpful_400_on_garbage(client):
    r = client.post("/api/accounts/1/cookies", json={"cookies_json": "not json at all"})
    assert r.status_code == 400
    assert "auth_token" in r.json()["detail"]


def test_create_account_accepts_bare_token(client):
    r = client.post("/api/accounts", json={"handle": "newbie",
                                           "cookies_json": TOKEN})
    assert r.status_code == 200, r.text
    assert db.account_cookies(r.json()["account_id"]) == CANON
    # garbage still rejected — validation not weakened
    r = client.post("/api/accounts", json={"handle": "newbie2",
                                           "cookies_json": "garbage"})
    assert r.status_code == 400
    assert "auth_token" in r.json()["detail"]


def test_cookie_connect_accepts_bare_token(client, monkeypatch):
    monkeypatch.setattr(xclient, "XCookie",
                        lambda c, caps=None, account_id=1: _FakeProbe(c))
    r = client.post("/api/x/cookie-connect", json={"cookies_json": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "probe_user"
    assert db.account_cookies(db.active_account()) == CANON


# ---------------- .env bootstrap through the same normalizer ----------------

def test_resolve_cookies_env_bare_token(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "env.db")
    db.init_db()
    monkeypatch.setenv("OPENSTANLEY_X_COOKIES", TOKEN)
    cfg = Config()
    # bare .env token now resolves to canonical JSON for account 1 only
    assert xclient.resolve_cookies(cfg, 1) == CANON
    a2 = db.create_account("second")
    assert xclient.resolve_cookies(cfg, a2) == ""
    # the DB row still wins over .env
    db.set_account_cookies(1, '{"auth_token": "db-token", "ct0": "d"}')
    assert '"db-token"' in xclient.resolve_cookies(cfg, 1)


def test_resolve_cookies_env_garbage_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "env2.db")
    db.init_db()
    monkeypatch.setenv("OPENSTANLEY_X_COOKIES", "garbage")
    cfg = Config()
    assert xclient.resolve_cookies(cfg, 1) == ""
