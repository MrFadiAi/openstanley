"""FIX_BRIEF_BOOTSTRAP_VALIDATION — connect/bootstrap must never persist
unvalidated or healed cookies.

The bug being pinned: XCookie.me() auto-heals on auth failure (pulls real
cookies from the logged-in Brave over CDP). A bootstrap with a FAKE token
could therefore succeed on the healed session and the endpoint would persist
the ORIGINAL fake input as if it had been validated.

Rules under test — for EVERY endpoint that persists cookies:
  * POST /api/accounts/bootstrap        (paste → create/reconnect account)
  * POST /api/x/cookie-connect          (cookie wizard)
  * POST /api/accounts/{id}/cookies     (settings accounts card)
  * POST /api/accounts                  (create with cookies pasted up front)
  1. identity validation runs with heal DISABLED — if the heal machinery is
     touched during validation the test fails
  2. fake token → HTTP 400, stored cookies byte-identical before/after, and
     no account row is created as a side effect
  3. valid token → exactly the submitted canonical cookies are stored, and
     the probe was called with heal=False

Hermetic: fake twikit module (REAL XCookie, no network), heal spies that
record every touch, OPENSTANLEY_TEST_DB, sandboxed brains (conftest).
"""
from __future__ import annotations

import json
import os
import sys
import types

os.environ.setdefault("OPENSTANLEY_NO_SCHEDULER", "1")  # before importing the server

import pytest  # noqa: E402

from openstanley.core import db                      # noqa: E402
db.init_db()

import openstanley.server.__main__ as server         # noqa: E402
import openstanley.x.client as xclient               # noqa: E402
from openstanley.x.client import normalize_cookies_input  # noqa: E402
from openstanley.x import cookie_heal                # noqa: E402

# a token that normalizes fine but would never authenticate on real X
FAKE_TOKEN = "f" * 40
FAKE_CANON = json.dumps({"auth_token": FAKE_TOKEN}, separators=(",", ":"))
# what account 1 holds before each attempt — must survive a rejected paste
SENTINEL = json.dumps({"auth_token": "s" * 40, "ct0": "sentinel"}, separators=(",", ":"))


# ---------------- hermetic seams ------------------------------------------------

class _AuthFailTwikit:
    """twikit stand-in: every user() call dies with X's code-32 auth error —
    exactly what a fake/rotated auth_token produces on real X."""
    def __init__(self, lang: str):
        self._ck: dict = {}

    def set_cookies(self, cookies: dict) -> None:  # SYNC in twikit 2.3.3
        self._ck = cookies

    async def user(self):
        raise Exception('status: 401, message: "{"errors":[{"code":32}]}"')


@pytest.fixture()
def auth_fail_x(monkeypatch):
    """Real XCookie + fake twikit → me() hits a genuine auth failure."""
    fake_mod = types.ModuleType("twikit")
    fake_mod.Client = _AuthFailTwikit
    monkeypatch.setitem(sys.modules, "twikit", fake_mod)


@pytest.fixture()
def heal_spies(monkeypatch):
    """Record every touch of the heal machinery. handle_failure is the only
    entry the _auto_heal wrapper calls; heal_cookies/pull are belt-and-braces
    for future refactors. Validation paths must leave ALL counts at zero."""
    touches = {"handle_failure": 0, "heal_cookies": 0, "pull": 0}

    async def _handle_failure(*args, **kwargs):
        touches["handle_failure"] += 1
        return False  # "heal unavailable" — the auth error must propagate

    async def _heal_cookies(*args, **kwargs):
        touches["heal_cookies"] += 1
        return False

    def _pull(*args, **kwargs):
        touches["pull"] += 1
        return None

    monkeypatch.setattr(cookie_heal, "handle_failure", _handle_failure)
    monkeypatch.setattr(cookie_heal, "heal_cookies", _heal_cookies)
    monkeypatch.setattr(cookie_heal, "pull_cookies_from_browser", _pull)
    return touches


class _CsrfFailTwikit:
    """twikit stand-in: user() dies with X's code-353 CSRF error — what a
    bare auth_token paste (no ct0) produces on real X."""
    def __init__(self, lang: str):
        self._ck: dict = {}

    def set_cookies(self, cookies: dict) -> None:  # SYNC in twikit 2.3.3
        self._ck = cookies

    async def user(self):
        raise Exception('status: 403, message: "{"errors":[{"code":353,'
                        '"message":"This request requires a matching csrf '
                        'cookie and header."}]}"')


@pytest.fixture()
def csrf_fail_x(monkeypatch):
    fake_mod = types.ModuleType("twikit")
    fake_mod.Client = _CsrfFailTwikit
    monkeypatch.setitem(sys.modules, "twikit", fake_mod)


class _OkProbe:
    """XCookie-shaped stub for the VALID-token path — records the heal kwarg
    so tests can prove validation asked for a no-heal check."""
    instances: list["_OkProbe"] = []

    def __init__(self, cookies_json: str, caps=None, account_id: int = 1):
        self._cookies = cookies_json
        self.heal_calls: list[bool] = []
        _OkProbe.instances.append(self)

    async def me(self, heal: bool = True) -> dict:
        self.heal_calls.append(heal)
        return {"username": "valid_handle", "name": "Valid Handle", "followers": 12}


@pytest.fixture()
def ok_probe(monkeypatch):
    _OkProbe.instances = []
    monkeypatch.setattr(xclient, "XCookie", _OkProbe)


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
        db.set_active_account(1)
    _clean()
    yield
    _clean()


def _accounts_snapshot() -> list[tuple]:
    return [(a["id"], (a["handle"] or "").lower()) for a in db.list_accounts()]


# ---------------- rule 1+2: fake token → 400, DB untouched, no heal -------------

def test_bootstrap_fake_token_rejected_no_heal_no_write(client, auth_fail_x, heal_spies):
    db.set_account_cookies(1, SENTINEL)
    before_accounts = _accounts_snapshot()
    r = client.post("/api/accounts/bootstrap", json={"cookies_json": FAKE_TOKEN})
    assert r.status_code == 400, r.text
    assert "re-copy" in r.json()["detail"] or "rejected" in r.json()["detail"]
    assert db.account_cookies(1) == SENTINEL          # stored cookies untouched
    assert _accounts_snapshot() == before_accounts    # no ghost account created
    assert heal_spies == {"handle_failure": 0, "heal_cookies": 0, "pull": 0}


def test_bootstrap_csrf_353_gets_ct0_hint_not_token_blame(client, csrf_fail_x, heal_spies):
    """code-353 = ct0 missing/stale, NOT a bad auth_token — the message must
    say exactly that, or users re-copy a perfectly valid token forever."""
    db.set_account_cookies(1, SENTINEL)
    r = client.post("/api/accounts/bootstrap", json={"cookies_json": FAKE_TOKEN})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "ct0" in detail
    assert "auth_token=…; ct0=…" in detail          # shows the paste format
    assert "invalid or expired" not in detail       # must NOT blame the token
    assert db.account_cookies(1) == SENTINEL        # nothing persisted
    assert heal_spies == {"handle_failure": 0, "heal_cookies": 0, "pull": 0}


def test_cookie_connect_fake_token_rejected_no_heal_no_write(client, auth_fail_x, heal_spies):
    db.set_account_cookies(1, SENTINEL)
    before_accounts = _accounts_snapshot()
    r = client.post("/api/x/cookie-connect", json={"cookies_json": FAKE_TOKEN})
    assert r.status_code == 400, r.text
    assert db.account_cookies(1) == SENTINEL          # active account untouched
    assert _accounts_snapshot() == before_accounts
    assert heal_spies == {"handle_failure": 0, "heal_cookies": 0, "pull": 0}


def test_set_account_cookies_fake_token_rejected_no_heal_no_write(client, auth_fail_x, heal_spies):
    db.set_account_cookies(1, SENTINEL)
    r = client.post("/api/accounts/1/cookies", json={"cookies_json": FAKE_TOKEN})
    assert r.status_code == 400, r.text
    assert db.account_cookies(1) == SENTINEL          # previous cookies survive
    assert heal_spies == {"handle_failure": 0, "heal_cookies": 0, "pull": 0}


def test_create_account_fake_token_rejected_no_orphan(client, auth_fail_x, heal_spies):
    db.set_account_cookies(1, SENTINEL)
    before_accounts = _accounts_snapshot()
    r = client.post("/api/accounts", json={"handle": "ghost",
                                           "cookies_json": FAKE_TOKEN})
    assert r.status_code == 400, r.text
    assert _accounts_snapshot() == before_accounts    # validation precedes create
    assert heal_spies == {"handle_failure": 0, "heal_cookies": 0, "pull": 0}


# ---------------- rule 3: valid token → exactly the submitted cookies stored ----

def test_bootstrap_valid_token_stores_submitted_cookies(client, ok_probe):
    db.set_account_cookies(1, SENTINEL)
    raw = f"auth_token={FAKE_TOKEN}; ct0=fresh"       # paste whatever you have
    r = client.post("/api/accounts/bootstrap", json={"cookies_json": raw})
    assert r.status_code == 200, r.text
    canonical = normalize_cookies_input(raw)
    acct = r.json()["account_id"]
    # EXACTLY what was validated is stored — not a healed variant, not the raw
    # paste, byte-identical to the canonical string handed to the probe
    assert db.account_cookies(acct) == canonical
    assert db.account_cookies(acct) == _OkProbe.instances[0]._cookies
    assert _OkProbe.instances[0].heal_calls == [False]  # validation was no-heal


def test_cookie_connect_valid_token_stores_submitted_cookies(client, ok_probe):
    raw = f"auth_token={FAKE_TOKEN}; ct0=fresh"
    r = client.post("/api/x/cookie-connect", json={"cookies_json": raw})
    assert r.status_code == 200, r.text
    assert db.account_cookies(db.active_account()) == \
        normalize_cookies_input(raw)
    assert _OkProbe.instances[0].heal_calls == [False]


def test_set_account_cookies_valid_token_stores_submitted_cookies(client, ok_probe):
    raw = f"auth_token={FAKE_TOKEN}; ct0=fresh"
    r = client.post("/api/accounts/1/cookies", json={"cookies_json": raw})
    assert r.status_code == 200, r.text
    assert db.account_cookies(1) == normalize_cookies_input(raw)
    assert _OkProbe.instances[0].heal_calls == [False]


def test_create_account_valid_token_stores_submitted_cookies(client, ok_probe):
    raw = f"auth_token={FAKE_TOKEN}; ct0=fresh"
    r = client.post("/api/accounts", json={"handle": "valid_handle",
                                           "cookies_json": raw})
    assert r.status_code == 200, r.text
    assert db.account_cookies(r.json()["account_id"]) == \
        normalize_cookies_input(raw)
    assert _OkProbe.instances[0].heal_calls == [False]


# ---------------- rule 4: the no-heal seam itself -------------------------------

def test_me_heal_false_propagates_auth_error_without_healing(monkeypatch):
    """XCookie.me(heal=False) must let the auth error through untouched —
    the seam every validation endpoint relies on (unit level)."""
    import asyncio
    fake_mod = types.ModuleType("twikit")
    fake_mod.Client = _AuthFailTwikit
    monkeypatch.setitem(sys.modules, "twikit", fake_mod)

    async def _fail(*args, **kwargs):
        raise AssertionError("heal must not run when heal=False")

    monkeypatch.setattr(cookie_heal, "handle_failure", _fail)
    xc = xclient.XCookie(FAKE_CANON)
    with pytest.raises(Exception, match="code 32|401"):
        asyncio.run(xc.me(heal=False))
