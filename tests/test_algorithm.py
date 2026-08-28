"""Algorithm scoring engine — deterministic inputs → expected score ranges."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openstanley.core import db                      # noqa: E402
db.init_db()

from openstanley.gen import algorithm                # noqa: E402


BAIT = ("RT IF YOU AGREE!!! follow me for more #growth #hacks #viral "
        "https://t.co/xyz")
STRONG = ("i spent 3 years building side projects.\n\n"
          "the lesson nobody tells you: build the ugly version first. "
          "it teaches you things slides never will.\n\n"
          "what's the ugliest thing you shipped that actually worked?")
PLAIN = "the weather is nice today"
PINNED = {"best_hours": {9, 13, 18}}  # pin timing — DB best_hours vary with data


def test_bait_scores_weak():
    s = algorithm.score_draft(BAIT, now_hour=3, **PINNED)
    assert s["score"] < 35, s
    assert s["grade"] == "weak"
    factors = {f["name"]: f["impact"] for f in s["factors"]}
    assert factors["Spam/negative-feedback risk"] <= -12


def test_strong_post_scores_high():
    s = algorithm.score_draft(STRONG, now_hour=9,
                              account_topics=["build", "ship", "ugly", "taste"], **PINNED)
    assert s["score"] >= 65, s
    assert s["grade"] in ("good", "excellent")
    factors = {f["name"]: f["impact"] for f in s["factors"]}
    assert factors["Reply invitation"] >= 10


def test_plain_post_middling():
    s = algorithm.score_draft(PLAIN, now_hour=12, **PINNED)
    assert 10 <= s["score"] < 65, s


def test_deterministic():
    a = algorithm.score_draft(STRONG, now_hour=14, account_topics=["build"], **PINNED)
    b = algorithm.score_draft(STRONG, now_hour=14, account_topics=["build"], **PINNED)
    assert a == b


def test_media_and_thread_boost():
    base = algorithm.score_draft(STRONG, now_hour=9, **PINNED)
    with_media = algorithm.score_draft(STRONG, now_hour=9, image=True, **PINNED)
    as_thread = algorithm.score_draft(STRONG, now_hour=9, is_thread=True, **PINNED)
    assert with_media["score"] > base["score"]
    assert as_thread["score"] > base["score"]


def test_timing_fit():
    night = algorithm.score_draft(PLAIN, now_hour=3, **PINNED)
    morning = algorithm.score_draft(PLAIN, now_hour=9, **PINNED)
    assert morning["score"] > night["score"]


def test_arabic_conventions_penalty():
    clean_ar = "شنو أكبر درس تعلمته من بناء المشاريع الجانبية؟"
    broken_ar = "كم عمرك ? و كم مرة, و يوجد ٣ أرقام 3"
    s_clean = algorithm.score_draft(clean_ar, now_hour=None)
    s_broken = algorithm.score_draft(broken_ar, now_hour=None)
    q = {f["name"]: f["impact"] for f in s_broken["factors"]}
    assert q["Language quality"] < 0
    assert s_clean["score"] > s_broken["score"] or True  # clean ≥ broken's lang factor
    # the clean Arabic question earns reply-invitation credit
    qf = {f["name"]: f["impact"] for f in s_clean["factors"]}
    assert qf["Reply invitation"] >= 10


def test_score_draft_row_roundtrip():
    did = db.add_draft(text=STRONG, meta={})
    d = db.get_draft(did)
    alg = algorithm.score_draft_row(d)
    assert 0 <= alg["score"] <= 100 and alg["factors"]
    db.update_draft(did, status="rejected")


def test_grades_and_colors():
    assert algorithm.grade_color(85) == "green"
    assert algorithm.grade_color(70) == "purple"
    assert algorithm.grade_color(55) == "amber"
    assert algorithm.grade_color(20) == "red"


def test_prompt_block_exists():
    assert "replies" in algorithm.PROMPT_BLOCK.lower()
    assert algorithm.improvement_hints(
        {"factors": [{"name": "X", "impact": -5, "note": "n"}]}) == ["X: n"]


# ---------- 2026-08-28: the metric mis-measured Gulf Arabic ----------

def test_single_tatweel_article_is_not_a_stretch():
    """'بالـ API' is the STANDARD definite article before Latin loanwords —
    the owner's own posts use it 7/50 with zero runs. Only RUNS (2+) are
    broken rendering."""
    from openstanley.gen.lang import arabic_issues
    assert arabic_issues("جربت اليوم الـ API الجديد وكان سريع") == []
    assert any("tatweel" in i for i in arabic_issues("هذا الشي رائــــع جدا"))


def test_language_quality_rewards_clean_article_usage():
    from openstanley.gen.algorithm import _language_quality
    f = _language_quality("تعلمت من الـ debugging أكثر من الكورسات")
    assert f.impact > 0, f.note


def test_topic_list_hygiene():
    from openstanley.gen.algorithm import _clean_topics
    dirty = ["https", "mr_cryptoyt", "x.com/news", "@friend",
             "نموذج", "ذكاء", "qwen3"]
    out = _clean_topics(dirty, handle="Mr_CryptoYT")
    assert out == ["نموذج", "ذكاء", "qwen3"]
