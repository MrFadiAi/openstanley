"""The X read path — twikit 2.3.3 compat patches (offline; no network, no cookies).

Covers the KEY_BYTE fix: X rotated the webpack chunk manifest out of its
home-page HTML, so twikit's ClientTransaction.init() raised
"Couldn't get KEY_BYTE indices" on every request. Our patch (see
openstanley/x/twikit_patch.py) replaces get_indices with a two-step lookup
(d60/twikit#432 logic) plus graceful degradation: when the manifest can't be
found, requests proceed WITHOUT X-Client-Transaction-Id instead of dying.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

from openstanley.x import twikit_patch  # noqa: E402

# --- synthetic HTML/JS shapes (both X bundle generations) ---
NEW_FORMAT_HTML = '<html><script>chunkMap={,1234:"ondemand.s",1234:"a1b2c3d4e5f6"};</script></html>'
LEGACY_FORMAT_HTML = '<html><script>files={"ondemand.s":"ab12cd34ef"}</script></html>'
NO_MANIFEST_HTML = "<html><body>logged-out x-web page, no chunk map</body></html>"
ONDEMAND_JS_NEW = "keyBytes=([t[5],16)([t[42],16)([t[45],16)"
ONDEMAND_JS_LEGACY = "(e[2],16)+(e[42],16)+(e[45],16)"


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)


class _FakeSession:
    def __init__(self, text: str):
        self._text = text
        self.requested: list[str] = []

    async def request(self, method: str, url: str, headers=None):
        self.requested.append(url)
        return _FakeResponse(self._text)


def _ct():
    """A ClientTransaction with the patched get_indices installed."""
    twikit_patch.apply()
    from twikit.x_client_transaction.transaction import ClientTransaction
    return ClientTransaction()


def test_patch_applies_idempotently():
    first = twikit_patch.apply()
    # either fresh (first call in this process) or already-applied names
    assert set(first) <= {"get_indices", "request-degrades-without-transaction-id",
                          "user-guards", "guest-user-guards"}
    assert twikit_patch.apply() == []  # second call is a no-op


def test_get_indices_new_webpack_format():
    ct = _ct()
    session = _FakeSession(ONDEMAND_JS_NEW)
    row, rest = asyncio.run(ct.get_indices(NEW_FORMAT_HTML, session, {}))
    assert row == 5
    assert rest == [42, 45]
    assert session.requested == [
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.a1b2c3d4e5f6a.js"]


def test_get_indices_legacy_format_fallback():
    ct = _ct()
    session = _FakeSession(ONDEMAND_JS_LEGACY)
    row, rest = asyncio.run(ct.get_indices(LEGACY_FORMAT_HTML, session, {}))
    assert row == 2
    assert rest == [42, 45]


def test_get_indices_degrades_without_raising():
    """The actual 2.3.3 bug: no manifest → must not raise KEY_BYTE."""
    ct = _ct()
    row, rest = asyncio.run(ct.get_indices(NO_MANIFEST_HTML, _FakeSession(""), {}))
    assert (row, rest) == twikit_patch._PLACEHOLDER
    assert ct.degraded is True


def test_user_guards_survive_sparse_legacy_payload():
    twikit_patch.apply()
    from twikit.user import User
    sparse = {
        "rest_id": "123",
        "is_blue_verified": False,
        "legacy": {
            "screen_name": "acct", "name": "Acct", "location": "",
            "description": "", "verified": False, "possibly_sensitive": False,
            "followers_count": 10, "followees_count": 5, "media_count": 0,
            "statuses_count": 100, "favourites_count": 0, "translator_type": "none",
            "is_translator": False, "created_at": "2020-01-01",
            "profile_image_url_https": "https://pbs.twimg.com/x.jpg",
            "can_dm": True, "can_media_tag": True, "want_retweets": True,
            "default_profile": True, "default_profile_image": False,
            "has_custom_timelines": False, "fast_followers_count": 0,
            "normal_followers_count": 10, "friends_count": 5, "listed_count": 0,
            # entities.description.urls / pinned_tweet_ids_str /
            # withheld_in_countries intentionally ABSENT (new X behavior)
        },
    }

    class _NoClient:
        pass

    u = User(_NoClient(), sparse)  # type: ignore[arg-type]
    assert u.description_urls == []
    assert u.pinned_tweet_ids == []
    assert u.withheld_in_countries == []


def test_vendored_request_omits_transaction_header_when_degraded():
    """Degraded mode must send NO X-Client-Transaction-Id (never a bogus one)."""
    twikit_patch.apply()
    from twikit.client.client import Client

    sent_headers: dict[str, Any] = {}

    class _RecordingHttp:
        async def request(self, method, url, headers=None, **kw):
            sent_headers.update(headers or {})
            return _FakeResponse("{}")

    class _CT:
        home_page_response = None
        degraded = True

        async def init(self, session, headers):
            self.home_page_response = True

        def generate_transaction_id(self, **kw):
            raise AssertionError("must not be called in degraded mode")

    c = Client.__new__(Client)  # bypass __init__ (no network)
    c.http = _RecordingHttp()
    c.client_transaction = _CT()
    c.language = "en-US"
    c._user_agent = "ua"
    c.captcha_solver = None
    c.get_cookies = lambda: {}
    c.set_cookies = lambda *a, **k: None
    c._remove_duplicate_ct0_cookie = lambda: None

    out = asyncio.run(c.request("GET", "https://x.com/i/api/1.1/foo"))
    assert out[0] == {}
    assert "X-Client-Transaction-Id" not in sent_headers


def test_vendored_request_sets_transaction_header_when_computable():
    twikit_patch.apply()
    from twikit.client.client import Client

    sent_headers: dict[str, Any] = {}

    class _RecordingHttp:
        async def request(self, method, url, headers=None, **kw):
            sent_headers.update(headers or {})
            return _FakeResponse(json.dumps({"data": {}}))

    class _CT:
        home_page_response = True  # already initialized → no re-init
        degraded = False

        def generate_transaction_id(self, **kw):
            return "abc123"

    c = Client.__new__(Client)
    c.http = _RecordingHttp()
    c.client_transaction = _CT()
    c.language = "en-US"
    c._user_agent = "ua"
    c.captcha_solver = None
    c.get_cookies = lambda: {}
    c.set_cookies = lambda *a, **k: None
    c._remove_duplicate_ct0_cookie = lambda: None

    asyncio.run(c.request("GET", "https://x.com/i/api/1.1/foo"))
    assert sent_headers.get("X-Client-Transaction-Id") == "abc123"


def test_cookie_client_wires_patch_on_ensure(monkeypatch):
    """XCookie._ensure applies the compat patches before building the client."""
    import openstanley.x.client as xclient
    calls: list[str] = []

    class _FakeTwikitClient:
        def __init__(self, lang):
            pass

        def set_cookies(self, cookies):
            pass

        class _Me:
            screen_name = "someone"
            name = "Some One"
            followers_count = 1234

        async def user(self):
            return _FakeTwikitClient._Me()

    import types
    import openstanley.core.db as real_db
    logged: list[str] = []
    monkeypatch.setattr(real_db, "log", lambda *a, **k: logged.append(a))
    fake_mod = types.ModuleType("twikit")
    fake_mod.Client = _FakeTwikitClient
    monkeypatch.setitem(__import__("sys").modules, "twikit", fake_mod)

    xc = xclient.XCookie(json.dumps({"auth_token": "x" * 40, "ct0": "y" * 40}))
    me = asyncio.run(xc.me())
    assert me["username"] == "someone"
    # patches active in this process (fresh application is logged; repeat
    # applications are idempotent no-ops, so accept either evidence)
    assert twikit_patch.applied, "compat patches should be active"
    assert logged or twikit_patch.apply() == []


def test_cookie_get_tweet_uses_renamed_twikit_method(monkeypatch):
    """twikit 2.x renamed get_tweet → get_tweet_by_id. Live 2026-08-31:
    the owner's 'quote it' died with "'Client' object has no attribute
    'get_tweet'" — no tweet could ever be fetched for quoting."""
    from openstanley.x.client import XCookie

    class _FakeUser:
        screen_name = "ashcrypto"

    class _FakeTweet:
        id = "2094123456789"
        text = "breaking: japan stablecoin tax news"
        user = _FakeUser()

    class _FakeTwikit:  # the 2.3.3 shape: get_tweet_by_id ONLY
        async def get_tweet_by_id(self, x_id):
            assert x_id == "2094123456789"
            return _FakeTweet()

    xc = XCookie("{}", username="mr")

    async def _fake_ensure():
        return _FakeTwikit()

    async def _fake_throttle():
        return None

    monkeypatch.setattr(xc, "_ensure", _fake_ensure)
    monkeypatch.setattr(xc, "_throttle_reads", _fake_throttle)
    t = asyncio.run(xc.get_tweet("2094123456789"))
    assert t["x_id"] == "2094123456789"
    assert t["author"] == "ashcrypto"
    assert t["text"].startswith("breaking")


def test_tw_maps_url():
    """Search results carry a ready-built URL so quote/reply flows never
    have to guess a link from t.co fragments (live 2026-08-31: 'no URLs
    came back in the results' blocked a quote retry)."""
    from openstanley.x.client import XCookie

    class _U:
        screen_name = "coindesk"

    class _T:
        id = "999"
        text = "x"
        user = _U()
        favorite_count = "3"
        retweet_count = None

    d = XCookie._tw(_T())
    assert d["url"] == "https://x.com/coindesk/status/999"
    assert d["author_handle"] == "coindesk"
