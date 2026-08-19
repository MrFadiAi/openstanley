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
