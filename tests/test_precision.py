"""Precision mode — fewer, better drafts, pre-vetted against every
rejection rule (owner data: 22% approval rate drove this)."""
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

from openstanley.gen import precision                              # noqa: E402
from openstanley.gen import brain as brain_mod                     # noqa: E402


def test_rule_violation_detected():
    """A draft matching a learned DON'T rule at >=60% token overlap is
    skipped — the owner already refused this pattern."""
    brain_mod.add_rule("DON'T write meta-analysis of influencers "
                       "marketing moves link drops trust hacks",
                       "rejection", acct=1)
    ok, why = precision.preflight(
        "تحليل لاستراتيجية المؤثرين: link drops و trust hacks — كيف يشتغلون",
        acct=1)
    # Arabic draft vs English rule — tokens differ; use an English case
    ok2, why2 = precision.preflight(
        "meta-analysis of influencer marketing moves: link drops and "
        "trust hacks explained", acct=1)
    assert not ok2, f"should match the rejection rule: {why2}"
    assert "R" in why2


def test_clean_draft_passes():
    brain_mod.add_rule("DON'T write meta analysis of crypto influencer "
                       "shill accounts ever again", "rejection", acct=1)
    ok, why = precision.preflight(
        "جربت الـ AI film studio هسه، النتيجة تجنن 🔥", acct=1)
    assert ok, why


def test_sameness_gate():
    ok, why = precision.preflight(
        "the exact same words repeated verbatim here",
        acct=1, recent_drafts=["the exact same words repeated verbatim here"])
    assert not ok and "similar" in why


def test_precision_max_is_tight():
    assert precision.PRECISION_MAX <= 2, \
        "precision mode means at most 2 cards per run"
