"""X read-path transient retry — the three-incident 404/429 armor.

Evidence (2026-08-27): smoke identity 404 (code 34, fresh client OK seconds
later), recovery me() 404, timeline 429s — read calls died on flakiness the
auth-heal decorator was never meant to catch. Writes are NEVER retried
(duplicate-post risk): _read_retry is applied to read methods only.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.x.client import _is_transient_read_error, _read_retry  # noqa: E402


class _NotFound(Exception):
    pass


class _TooManyRequests(Exception):
    pass


def test_transient_classification():
    e404 = _NotFound('status: 404, message: "{"errors":[{"message":"Sorry, '
                     'that page does not exist","code":34}]}"')
    assert _is_transient_read_error(e404) is True
    assert _is_transient_read_error(_TooManyRequests("429 rate")) is True
    assert _is_transient_read_error(RuntimeError("HTTP 500")) is False
    assert _is_transient_read_error(_NotFound("status: 404 plain")) is False, \
        "a 404 WITHOUT code 34 (deleted tweet) is permanent — no retry"


def _flaky(fails: int, exc: Exception):
    state = {"n": 0}

    class Probe:
        @_read_retry(wait_429_s=0.01, wait_404_s=0.01)
        async def call(self):
            state["n"] += 1
            if state["n"] <= fails:
                raise exc
            return "ok"
    return Probe(), state


def test_transient_404_retried_once_and_succeeds():
    probe, state = _flaky(1, _NotFound(
        'status: 404, message: "{"errors":[{"code":34}]}"'))
    assert asyncio.run(probe.call()) == "ok"
    assert state["n"] == 2


def test_rate_limit_retried_once_and_succeeds():
    probe, state = _flaky(1, _TooManyRequests("429"))
    assert asyncio.run(probe.call()) == "ok"
    assert state["n"] == 2


def test_non_transient_raises_immediately():
    probe, state = _flaky(5, RuntimeError("HTTP 500"))
    try:
        asyncio.run(probe.call())
        raise AssertionError("should raise")
    except RuntimeError:
        pass
    assert state["n"] == 1, "no retry burn on permanent errors"


def test_persistent_transient_raises_after_one_retry_no_loop():
    e = _NotFound('status: 404, message: "{"code":34}"')
    probe, state = _flaky(99, e)
    try:
        asyncio.run(probe.call())
        raise AssertionError("should raise")
    except _NotFound:
        pass
    assert state["n"] == 2, "exactly one retry — never a loop"
