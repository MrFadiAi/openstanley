"""Cookie auto-heal — hermetic tests (no network, no browser, no real X).

Covers the four pieces of openstanley/x/cookie_heal.py:
  * detector: auth-failure classification (fake exceptions → stale/not-stale)
  * .env persistence: atomic rewrite preserving foreign lines
  * cooldown: one heal attempt per 10 min, never a loop
  * healer: CDP pull (mocked), client rebuild, me-cache reset, graceful fail

The CDP pull (pull_cookies_from_browser) is mocked everywhere; nothing here
touches 127.0.0.1:9222 or x.com.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("OPENSTANLEY_NO_SCHEDULER", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.x import cookie_heal                              # noqa: E402

TEST_ENV_VAR = "OPENSTANLEY_TEST_HEAL_COOKIES"
GOOD_TOKEN, BAD_TOKEN = "g" * 40, "b" * 40


class _FakeUser:
    screen_name = "orbexai"
    name = "Orb Exai"
    followers_count = 421


# --- fixtures -------------------------------------------------------------------

@pytest.fixture()
def heal_state():
    """Isolated HealState + clean env var per test (restored after)."""
    saved = (cookie_heal.STATE.stale, cookie_heal.STATE.last_attempt,
             cookie_heal.STATE.last_heal, cookie_heal.STATE.heal_ok)
    cookie_heal.STATE.stale = False
    cookie_heal.STATE.last_attempt = None
    cookie_heal.STATE.last_heal = None
    cookie_heal.STATE.heal_ok = None
    os.environ.pop(TEST_ENV_VAR, None)
    yield cookie_heal.STATE
    (cookie_heal.STATE.stale, cookie_heal.STATE.last_attempt,
     cookie_heal.STATE.last_heal, cookie_heal.STATE.heal_ok) = saved
    os.environ.pop(TEST_ENV_VAR, None)


@pytest.fixture()
def clock(monkeypatch):
    """Controllable monotonic clock for cooldown tests."""
    t = {"now": 1000.0}
    monkeypatch.setattr(cookie_heal, "_monotonic", lambda: t["now"])
    return t


@pytest.fixture()
def logs(monkeypatch):
    """Collect db.log calls made through cookie_heal for assertions."""
    entries: list[tuple] = []
    monkeypatch.setattr(db, "log", lambda loop, msg, level="info":
                        entries.append((loop, msg, level)))
    return entries


# --- detector -------------------------------------------------------------------

def test_detector_flags_auth_failures():
    authy = [
        Exception('status: 401, message: "could not authenticate you"'),
        Exception('status: 403, message: "{"errors":[{"code":353}]}"'),
        Exception("Unauthorized: code 32 — You must be logged in"),
        Exception("Couldn't get KEY_BYTE indices"),
        type("Unauthorized", (Exception,), {})("boom"),
        type("Forbidden", (RuntimeError,), {})("boom"),
    ]
    for exc in authy:
        assert cookie_heal.is_auth_failure(exc), f"should be stale: {exc!r}"


def test_detector_ignores_non_auth_failures():
    not_authy = [
        Exception('status: 500, message: "internal error"'),
        Exception("status: 429, message: rate limit"),
        Exception('status: 404, message: "not found"'),
        Exception('status: 400, message: "bad request code 320"'),
        ValueError("no json"),
        TimeoutError(),
        ConnectionError("connection reset"),
    ]
    for exc in not_authy:
        assert not cookie_heal.is_auth_failure(exc), f"not stale: {exc!r}"


def test_mark_stale_logs_once(heal_state, logs):
    cookie_heal.mark_stale("first hit")
    assert heal_state.stale is True
    assert len(logs) == 1 and "stale" in logs[0][1]
    cookie_heal.mark_stale(Exception("status: 401, again"))
    assert heal_state.stale is True
    assert len(logs) == 1  # no re-log once already stale


# --- .env persistence -----------------------------------------------------------

def test_env_rewrite_preserves_foreign_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# openstanley secrets\n"
        "OPENSTANLEY_LLM_API_KEY=sk-abc\n"
        'OPENSTANLEY_X_COOKIES={"auth_token":"old"}\n'
        "\n"
        "OPENSTANLEY_X_MODE=cookie\n",
        encoding="utf-8")
    compact = json.dumps({"auth_token": "new", "ct0": "z"}, separators=(",", ":"))
    cookie_heal.persist_cookies_env(env, "OPENSTANLEY_X_COOKIES", compact)
    text = env.read_text(encoding="utf-8")
    assert "# openstanley secrets" in text
    assert "OPENSTANLEY_LLM_API_KEY=sk-abc" in text
    assert "OPENSTANLEY_X_MODE=cookie" in text
    assert 'OPENSTANLEY_X_COOKIES={"auth_token":"new","ct0":"z"}' in text
    assert "old" not in text
    assert not (tmp_path / ".env.tmp").exists()  # atomic: no temp left behind


def test_env_rewrite_appends_when_key_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n", encoding="utf-8")
    cookie_heal.persist_cookies_env(env, "OPENSTANLEY_X_COOKIES", '{"auth_token":"a"}')
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["OTHER=1", 'OPENSTANLEY_X_COOKIES={"auth_token":"a"}']


def test_env_rewrite_creates_missing_file(tmp_path):
    env = tmp_path / ".env"
    cookie_heal.persist_cookies_env(env, "OPENSTANLEY_X_COOKIES", '{"auth_token":"a"}')
    assert env.exists()
    assert env.read_text(encoding="utf-8") == 'OPENSTANLEY_X_COOKIES={"auth_token":"a"}\n'


# --- healer + cooldown ----------------------------------------------------------

class _FakeXC:
    """Minimal XCookie stand-in: just the two attributes heal mutates."""

    def __init__(self, cookies_json: str):
        self._cookies = cookies_json
        self._client = object()  # non-None → "built"


def _heal(xc, tmp_path):
    return asyncio.run(cookie_heal.heal_cookies(
        xc, env_var=TEST_ENV_VAR, env_path=tmp_path / ".env"))


def test_heal_rebuilds_client_on_rotated_token(heal_state, clock, logs, monkeypatch, tmp_path):
    monkeypatch.setattr(cookie_heal, "pull_cookies_from_browser",
                        lambda: {"auth_token": GOOD_TOKEN, "ct0": "c" * 32})
    xc = _FakeXC(json.dumps({"auth_token": BAD_TOKEN, "ct0": "old"}))
    cookie_heal.mark_stale("simulated 401")
    db.set_setting("me", {"username": "stale_identity", "followers": 1})

    assert _heal(xc, tmp_path) is True
    assert db.get_setting("me") is None  # me cache reset for re-verification
    fresh = json.loads(xc._cookies)
    assert fresh["auth_token"] == GOOD_TOKEN          # client rebuilt with new cookies
    assert xc._client is None                          # → next _ensure() re-creates
    assert os.environ[TEST_ENV_VAR] == xc._cookies     # in-memory env value updated
    assert (tmp_path / ".env").read_text(encoding="utf-8") == \
        f"{TEST_ENV_VAR}={json.dumps({'auth_token': GOOD_TOKEN, 'ct0': 'c' * 32}, separators=(',', ':'))}\n"
    assert heal_state.stale is False
    assert heal_state.heal_ok is True
    assert heal_state.last_heal is not None
    assert any("self-healed" in m and "auth_token rotated" in m for _, m, _ in logs)


def test_heal_refreshes_ct0_same_token(heal_state, clock, logs, monkeypatch, tmp_path):
    """ct0-only rotation (the ~daily case) must also rebuild the client."""
    monkeypatch.setattr(cookie_heal, "pull_cookies_from_browser",
                        lambda: {"auth_token": GOOD_TOKEN, "ct0": "n" * 32})
    xc = _FakeXC(json.dumps({"auth_token": GOOD_TOKEN, "ct0": "o" * 32}))
    assert _heal(xc, tmp_path) is True
    assert xc._client is None
    assert json.loads(os.environ[TEST_ENV_VAR])["ct0"] == "n" * 32
    assert any("ct0 refreshed" in m for _, m, _ in logs)


def test_heal_graceful_when_no_browser(heal_state, clock, logs, monkeypatch, tmp_path):
    monkeypatch.setattr(cookie_heal, "pull_cookies_from_browser", lambda: None)
    xc = _FakeXC(json.dumps({"auth_token": BAD_TOKEN}))
    cookie_heal.mark_stale("simulated 403")

    assert _heal(xc, tmp_path) is False
    assert heal_state.heal_ok is False
    assert heal_state.stale is True                 # still stale — nothing healed it
    assert not (tmp_path / ".env").exists()         # .env untouched
    assert xc._client is not None                   # client untouched
    assert any("no Brave" in m and "9222" in m for _, m, lvl in logs
               if lvl == "error")


def test_heal_cooldown_never_loops(heal_state, clock, monkeypatch, tmp_path):
    pulls = {"n": 0}
    monkeypatch.setattr(cookie_heal, "pull_cookies_from_browser",
                        lambda: (pulls.__setitem__("n", pulls["n"] + 1)
                                 or {"auth_token": GOOD_TOKEN, "ct0": "x"}))
    xc = _FakeXC(json.dumps({"auth_token": BAD_TOKEN}))

    assert _heal(xc, tmp_path) is True              # first attempt runs
    clock["now"] += 60                              # 1 min later — inside cooldown
    assert _heal(xc, tmp_path) is False             # blocked, no CDP pull
    clock["now"] += 60                              # 2 min total — still blocked
    assert _heal(xc, tmp_path) is False
    assert pulls["n"] == 1
    clock["now"] += cookie_heal.HEAL_COOLDOWN_S     # past 10 min
    assert _heal(xc, tmp_path) is True              # allowed again
    assert pulls["n"] == 2


# --- XCookie guard integration --------------------------------------------------

def test_xcookie_auto_heals_and_retries_once(monkeypatch, heal_state, clock, logs, tmp_path):
    """Full chain: 401 in _ensure → mark stale → heal (mocked CDP) → retry works."""
    import types
    import openstanley.x.client as xclient

    class _FakeTwikitClient:
        def __init__(self, lang: str):
            self._ck: dict = {}

        def set_cookies(self, cookies: dict) -> None:  # SYNC in twikit 2.3.3
            self._ck = cookies

        async def user(self):
            if self._ck.get("auth_token") != GOOD_TOKEN:
                raise Exception('status: 401, message: "{"errors":[{"code":32}]}"')
            return _FakeUser()

    fake_mod = types.ModuleType("twikit")
    fake_mod.Client = _FakeTwikitClient
    monkeypatch.setitem(sys.modules, "twikit", fake_mod)
    monkeypatch.setattr(cookie_heal, "pull_cookies_from_browser",
                        lambda: {"auth_token": GOOD_TOKEN, "ct0": "c" * 32})
    # keep the heal hermetic: no writes to the real .env / env var
    monkeypatch.setattr(cookie_heal, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(cookie_heal, "ENV_VAR", TEST_ENV_VAR)

    xc = xclient.XCookie(json.dumps({"auth_token": BAD_TOKEN, "ct0": "o" * 32}))
    me = asyncio.run(xc.me())  # first call 401s → heals → single retry succeeds
    assert me["username"] == "orbexai"
    assert heal_state.stale is False and heal_state.heal_ok is True
    assert json.loads(xc._cookies)["auth_token"] == GOOD_TOKEN
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert env_text.startswith(f"{TEST_ENV_VAR}=") and GOOD_TOKEN in env_text
    assert json.loads(os.environ[TEST_ENV_VAR])["auth_token"] == GOOD_TOKEN


def test_xcookie_write_not_auto_retried(monkeypatch, heal_state, clock, logs, tmp_path):
    """post_tweet marks stale + heals but re-raises (no double-post risk)."""
    import types
    import openstanley.core.safety as safety
    import openstanley.x.client as xclient

    async def _no_delay(*args, **kwargs):
        pass

    recorded: list[str] = []
    monkeypatch.setattr(safety, "human_delay", _no_delay)
    # counters live in the shared test DB — keep the cap check deterministic
    monkeypatch.setattr(safety, "check_and_record",
                        lambda kind, caps: recorded.append(kind))

    class _FakeTwikitClient:
        def __init__(self, lang: str):
            self._ck: dict = {}

        def set_cookies(self, cookies: dict) -> None:
            self._ck = cookies

        async def user(self):
            return _FakeUser()

        async def create_tweet(self, **kw):
            raise Exception("status: 403, message: forbidden code 353")

    fake_mod = types.ModuleType("twikit")
    fake_mod.Client = _FakeTwikitClient
    monkeypatch.setitem(sys.modules, "twikit", fake_mod)
    monkeypatch.setattr(cookie_heal, "pull_cookies_from_browser",
                        lambda: {"auth_token": GOOD_TOKEN, "ct0": "c" * 32})
    monkeypatch.setattr(cookie_heal, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(cookie_heal, "ENV_VAR", TEST_ENV_VAR)

    xc = xclient.XCookie(json.dumps({"auth_token": GOOD_TOKEN, "ct0": "o" * 32}),
                         caps={"max_posts_per_day": 1, "max_replies_per_day": 1,
                               "min_delay_s": 0, "max_delay_s": 0})
    with pytest.raises(Exception, match="code 353"):
        asyncio.run(xc.post_tweet("hello"))
    assert heal_state.heal_ok is True      # heal ran
    assert xc._client is None              # client rebuilt for the NEXT call
    assert recorded == ["posts"]           # no double-count from a phantom retry


# --- status endpoint ------------------------------------------------------------

def test_x_status_exposes_heal_fields(heal_state):
    from fastapi.testclient import TestClient
    from openstanley.server.__main__ import app
    with TestClient(app) as client:
        r = client.get("/api/x/status")
        assert r.status_code == 200
        body = r.json()
        assert body["cookies_stale"] is False
        assert body["last_heal"] is None
        assert body["heal_ok"] is None
