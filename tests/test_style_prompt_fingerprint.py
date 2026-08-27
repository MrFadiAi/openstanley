"""The style prompt must carry EVERY measured knob the drafts are judged on.

Live finding (2026-08-27): the block showed emoji/length/casing but omitted
sentence-length p50, question rate, the colon-led opener habit, and the
account's own vocabulary — three of the four style_distance sub-metrics were
invisible to the model. Voice suite scored 63.4 partly on those.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.gen import style_scan                             # noqa: E402


PROFILE = {
    "stats": {
        "avg_length_chars": 167,
        "emoji": {"per_post": 0.59, "top": ["😅"]},
        "hashtags": {"per_post": 0.2, "pct_with": 0.1},
        "casing": {"pct_lowercase_start": 0.8},
        "formatting": {"pct_multiline": 0.5},
        "language_mix": {"ar": 0.8, "en": 0.2},
        "sentence": {"p50": 6},
        "punctuation": {"colon": 0.685, "question": 0.12, "excl": 0.115},
        "vocabulary": {"top_terms": ["نموذج", "يعني", "ذكاء", "qwen3"]},
    },
    "human_summary": "Gulf Arabic tech voice.",
}


def _block() -> str:
    db.set_acct_setting("style_profile", PROFILE)
    return style_scan.style_prompt_block()


def test_block_carries_sentence_length_target():
    b = _block()
    assert "median 6 words" in b, "sentence p50 must reach the prompt"


def test_block_carries_punctuation_signature():
    b = _block()
    assert "colon-led" in b and "68%" in b, "the account's signature opener rate"
    assert "questions are RARE" in b and "12%" in b


def test_block_carries_account_vocabulary():
    b = _block()
    assert "نموذج" in b and "qwen3" in b


def test_low_colon_profile_omits_the_colon_line():
    p = dict(PROFILE)
    p["stats"] = {**PROFILE["stats"],
                  "punctuation": {"colon": 0.05, "question": 0.1, "excl": 0.1}}
    db.set_acct_setting("style_profile", p)
    b = style_scan.style_prompt_block()
    assert "colon-led" not in b, "no signature below 30% — don't force the habit"


def test_no_profile_is_empty_not_crash():
    db.set_acct_setting("style_profile", None)
    assert style_scan.style_prompt_block() == ""


def test_minimal_profile_without_punct_keys_renders():
    """Real bug: .get(k, 0) is not None is ALWAYS true, then punct[k]
    KeyError'd on profiles lacking the punctuation sub-dict keys (live test
    fixtures) — 5 suites broke. Missing key must skip the line, not crash."""
    p = {"stats": {"avg_length_chars": 120, "sentence": {"p50": 8}}}
    db.set_acct_setting("style_profile", p)
    b = style_scan.style_prompt_block()
    assert "median 8 words" in b
    assert "colon-led" not in b and "questions are RARE" not in b
