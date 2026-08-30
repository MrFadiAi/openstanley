"""Robustness tier — X error taxonomy, cookie countdown, precision cap."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.system.resilience import classify_x_error, cookie_health  # noqa: E402


def test_x_error_taxonomy():
    assert classify_x_error("{'code': 186}")[0] == "too long"
    assert classify_x_error("{'code': 326}")[0] == "duplicate"
    assert classify_x_error("{'code': 226}")[0] == "sensitive"
    assert classify_x_error("Authorization: cannot")[0] == "auth"
    assert classify_x_error("random nonsense") is None
    for code in ("186", "326", "226", "32"):
        kind, fix = classify_x_error(f"'{{'code': {code}}}'")
        assert fix, f"every known error must carry a fix path: {code}"


def test_cookie_health_shape():
    h = cookie_health(2)
    assert h["account"] == 2
    assert h["status"] in ("fresh", "aging", "stale", "unknown")
    if h["days_old"] is not None:
        assert h["days_old"] >= 0


def test_precision_cap():
    from openstanley.gen import precision
    assert precision.PRECISION_MAX <= 2
