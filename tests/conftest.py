"""Hermetic test environment — isolate ALL tests from real X auth.

Imported automatically by pytest before any test module. Guarantees:
  * OPENSTANLEY_X_COOKIES is unset (no real-account cookie client in tests)
  * X mode forced to dryrun (tests that need otherwise set it explicitly)
  * LLM calls stubbed at the httpx level unless OPENSTANLEY_TEST_LIVE_LLM=1
"""
from __future__ import annotations

import os

# must run before openstanley.core.config loads .env values into os.environ
os.environ.pop("OPENSTANLEY_X_COOKIES", None)
os.environ.pop("OPENSTANLEY_X_BEARER", None)
os.environ.pop("OPENSTANLEY_X_API_KEY", None)
# tests are ALWAYS hermetic: dryrun client regardless of data/config.toml
os.environ["OPENSTANLEY_X_MODE"] = "dryrun"
# v0.3.7: no startup smoke either — it would fire a real LLM probe using the
# .env key inside every TestClient test. test_system_smoke re-enables it with
# a faked run_smoke.
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
# v0.4.4: the Telegram poller must never long-poll real api.telegram.org
# from a TestClient boot; test_telegram starts it explicitly with faked httpx
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")
os.environ.setdefault("OPENSTANLEY_TEST_DB", os.path.join(os.path.dirname(__file__), "_data", "test.db"))
# v0.4.2 digest files land in the test sandbox, never the real data/digests
os.environ.setdefault("OPENSTANLEY_DIGEST_DIR", os.path.join(os.path.dirname(__file__), "_data", "digests"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _accounts_root_sandbox(tmp_path, monkeypatch):
    """v0.5.0: EVERY test gets a sandboxed data/accounts root — no test may
    ever read or write the install's real per-account brains (a test creating
    or deleting accounts through the API previously archived/moved REAL
    dirs). Tests that exercise the legacy migration patch ROOT consistently
    on top of this and are unaffected."""
    from openstanley.gen import brain
    monkeypatch.setattr(brain, "ACCOUNTS_ROOT", tmp_path / "accounts")
    yield


@pytest.fixture(autouse=True)
def _pin_active_account():
    """The ACTIVE ACCOUNT is shared DB state — whichever account a previous
    test (or previous SUITE RUN) switched to persists. Seeds written for
    account 1 were read back under account 2 on later runs (2026-08-28
    flake cluster: 5 insights tests + digest + deep_pull, pass-then-fail
    across identical code). Pin account 1 before every test; tests that
    deliberately switch accounts mid-run still can — this resets BETWEEN
    tests only."""
    from openstanley.core import db as _db
    with _db.connect() as _c:  # account 1 must exist for the pin to land
        _c.execute("INSERT OR IGNORE INTO accounts (id, handle, status) "
                   "VALUES (1, 'test-primary', 'active')")
    _db.set_active_account(1)
    yield
    _db.set_active_account(1)


@pytest.fixture(autouse=True)
def _watchdog_reset():
    """Chat watchdog state is a shared DB setting — every test starts clean
    so burst counters from one suite file can't trip guards in the next."""
    from openstanley.core import db as _db
    _db.set_setting("watchdog", {})
    yield
    _db.set_setting("watchdog", {})


@pytest.fixture(autouse=True)
def _instruction_llm_guard(monkeypatch):
    """The directive-capture seam (gen/instructions.llm_chat) is NOT covered
    by the per-test chat fakes other suites install — without this guard a
    gate-matching user message in any chat test would fire a REAL LLM call
    using the .env key (hermeticity incident class). Default: not-a-directive.
    Tests that exercise capture patch their own seam on top of this."""
    if os.environ.get("OPENSTANLEY_TEST_LIVE_LLM") == "1":
        return
    import json as _json
    from openstanley.gen import instructions as _im
    monkeypatch.setattr(
        _im, "llm_chat",
        lambda *a, **k: _json.dumps({"is_directive": False}))


def pytest_configure(config):
    """Loud hermeticity guard: if the DB path ever resolves back to the real
    data/openstanley.db (env regression, import-order change), fail the run
    before a single test can write production settings."""
    from openstanley.core import db as _db
    p = str(_db.DB_PATH).replace("\\", "/")
    assert "/tests/_data/" in p, (
        f"HERMETICITY BROKEN: tests would run against {p} — "
        "refusing to touch the real DB"
    )
