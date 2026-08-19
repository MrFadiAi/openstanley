"""Bilingual (AR/EN) — detection, conventions, prompts, voice match."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openstanley.core import db                      # noqa: E402
db.init_db()

from openstanley.gen import lang, voice, style_scan  # noqa: E402


AR_POST = "شنو أكبر درس تعلمته من بناء المشاريع الجانبية؟"
EN_POST = "what is the biggest lesson you learned from building side projects?"
MIXED = "أكبر درس تعلمته من المشاريع الجانبية: build the ugly version first"


def test_detection():
    assert lang.detect(AR_POST) == "ar"
    assert lang.detect(EN_POST) == "en"
    assert lang.detect(MIXED) == "mixed"
    assert lang.detect("") == "en"


def test_arabic_ratio():
    assert lang.arabic_ratio(AR_POST) > 0.7
    assert lang.arabic_ratio(EN_POST) < 0.1


def test_arabic_conventions_flagged():
    issues = lang.arabic_issues("كم عمرك ? و كم مرة, و يوجد ٣ أرقام 3")
    assert any("؟" in i for i in issues)
    assert any("،" in i for i in issues)
    assert any("numerals" in i for i in issues)
    # Persian lookalikes
    assert any("Persian" in i for i in lang.arabic_issues("هذا ی_'ک'_ نص ھ"))
    # clean Arabic → no issues
    assert lang.arabic_issues(AR_POST) == []
    # English text is never judged by Arabic rules
    assert lang.arabic_issues(EN_POST) == []


def test_numerals_style():
    assert lang.numerals_style("٣ أرقام") == "arabic-indic"
    assert lang.numerals_style("3 numbers") == "western"
    assert lang.numerals_style("٣ و 3") == "mixed"
    assert lang.numerals_style("none") == "none"


def test_chat_language_instruction():
    assert "Arabic" in lang.reply_language_instruction(AR_POST)
    assert "English" in lang.reply_language_instruction(EN_POST)


def test_generation_language_instructions():
    assert "ARABIC" in lang.draft_language_instruction("ar")
    assert lang.draft_language_instruction("en") == "\nWrite the post in ENGLISH."
    assert lang.draft_language_instruction(None) == ""


def _mk_profile(ar_pct: float = 0.5) -> dict:
    return {"stats": {
        "avg_length_chars": 120.0,
        "emoji": {"per_post": 0.1, "top": []},
        "hashtags": {"per_post": 0.0, "pct_with": 0.0},
        "casing": {"pct_lowercase_start": 0.9, "allcaps_words_per_post": 0.0},
        "language_mix": {"ar": ar_pct, "en": 1 - ar_pct, "mixed": 0.0},
    }}


def test_voice_match_fits_and_misfits():
    p = _mk_profile()
    good = style_scan.voice_match("a decent length post about building things", p)
    off_lang = style_scan.voice_match(AR_POST, _mk_profile(ar_pct=0.0))
    hashtags = style_scan.voice_match("great post #growth #hacking #viral", p)
    assert good > off_lang, (good, off_lang)
    assert hashtags < good
    assert 5 <= good <= 99


def test_bilingual_voice_prompt_block():
    db.save_voice('{"languages": {"ar": {"tone": "sharp"}, "en": {"tone": "dry"}}, "primary": "en"}',
                  [{"text": "example en", "likes": 1},
                   {"text": "مثال عربي", "likes": 2}])
    block_ar = voice.voice_prompt_block("ar")
    block_en = voice.voice_prompt_block("en")
    assert "sharp" in block_ar and "مثال عربي" in block_ar
    assert "dry" in block_en
    assert voice.rubric_for("ar") != voice.rubric_for("en")


def test_rubric_for_legacy_format():
    db.save_voice('{"tone": "legacy single"}', [])
    assert "legacy single" in voice.rubric_for(None)
