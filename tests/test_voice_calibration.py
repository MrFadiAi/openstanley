"""Voice suite is CALIBRATED against the account's own real posts.

Live finding (2026-08-27, account 2): real posts scored 61.7 on the suite's
own math while drafts scored 62.2 — the absolute scale is compressed and
dialect-blind, so "62" never meant "62% good". The suite now reports drafts
RELATIVE to ground truth: 100 = indistinguishable from (or better than) the
owner's own writing.

own_posts is patched at the seam: the shared test DB's active account is
whatever earlier suites left, so seeding rows and reading them back through
the ambient active account is not deterministic.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.core.config import Config                         # noqa: E402
from openstanley.harness.base import EvalContext                   # noqa: E402
from openstanley.harness.suites import voice_eval                  # noqa: E402

# a flat profile: nothing exotic, metrics behave deterministically
PROFILE = {"stats": {
    "avg_length_chars": 120,
    "emoji": {"per_post": 0, "top": []},
    "hashtags": {"per_post": 0, "pct_with": 0},
    "casing": {"pct_lowercase_start": 1.0},
    "formatting": {"pct_multiline": 0.0},
    "language_mix": {"en": 1.0},
    "sentence": {"p50": 8},
    "punctuation": {"question": 0.0, "excl": 0.0, "colon": 0.1},
}}

REAL = "calib real post text about shipping things quickly and honestly"


def _own_posts(rows: list[str]):
    return lambda limit=25, acct=None: [{"text": t} for t in rows]


def _ctx() -> EvalContext:
    def fake_llm(cfg, system="", user="", **kw):
        return json.dumps({"tweet": REAL})
    return EvalContext(cfg=Config(), llm=fake_llm, real=False,
                       use_brain=False, n=3, run_id=0, label="t")


def _run(monkeypatch, real_rows: list[str], draft_text: str | None = None):
    db.set_acct_setting("style_profile", PROFILE)
    monkeypatch.setattr(db, "own_posts", _own_posts(real_rows))
    monkeypatch.setattr(voice_eval, "sample_posts",
                        lambda ctx, n=None: [{"idea": "x",
                                              "text": draft_text or REAL}
                                             for _ in range(3)])
    return voice_eval.run(_ctx())


def test_score_calibrates_against_real_posts(monkeypatch):
    res = _run(monkeypatch, [REAL] * 8)
    # drafts == real posts exactly → 100 (indistinguishable from the owner)
    assert res["score"] == 100.0, res["score"]
    base = res["details"]["real_post_baseline"]
    assert base and base["posts"] == 8
    assert "calibrated" in res["details"]["note"]


def test_worse_than_baseline_scores_below_100(monkeypatch):
    worse = ("calib real post text about shipping things quickly and "
             "honestly but this one is far far longer than the profile "
             "average length by a very wide margin indeed")
    res = _run(monkeypatch, [REAL] * 8, draft_text=worse)
    assert res["score"] < 100.0, res["score"]


def test_drafts_exceeding_baseline_cap_at_100_with_note(monkeypatch):
    longer = REAL + " " + " ".join(f"word{i}" for i in range(30))
    # real baseline uses LONGER posts (worse metrics) → drafts exceed it
    res = _run(monkeypatch, [longer] * 8)
    assert res["score"] == 100.0
    assert "EXCEED" in res["details"]["note"]


def test_fresh_account_falls_back_to_absolute(monkeypatch):
    res = _run(monkeypatch, [])
    assert res["details"]["real_post_baseline"] is None
    assert res["score"] == res["details"]["mean_combined"]
    assert "absolute" in res["details"]["note"]


def test_baseline_under_five_posts_is_none(monkeypatch):
    monkeypatch.setattr(db, "own_posts", _own_posts([REAL] * 3))
    assert voice_eval.real_post_baseline(PROFILE) is None
