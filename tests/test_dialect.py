"""Dialect learning — the account's EXACT Arabic, mined from its posts.

Owner request 2026-08-28: "it has to be the perfect Arabic dialect,
dependent on the X account — study the account and know which sentences
it should use." Evidence on the live account: Iraqi at 0.89 confidence
(اني x29, شنو x19...). The block injects family + constructions + REAL
sentences + an avoid-list into every Arabic draft path.
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
from openstanley.gen import dialect as dia                         # noqa: E402

IRAQI_POSTS = [
    "اني صارلي فترة افكر بنفس الشي، وهسه قررت اسويه",
    "اكو ناس تظن الموضوع صعب بس هو اسهل من هيج",
    "جنت احاول افهم ليش صار هيج، طلعت المشكلة بالاساس مو بالكود",
    "هسه كل شي صار اسهل، شلونك انتة هالايام",
    "ماكو فايدة تنتظر الوقت المثالي، سويت و تعبت و تعلمت",
]
GULF_POSTS = [
    "ابي اسوي شي جديد هالفترة، ويش رايك",
    "الحين صار كل شي اوتوماتيك، مره حلو",
]


def _seed(*texts: str, acct: int = 1) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE is_own=1")
    for i, t in enumerate(texts):
        db.upsert_post({"x_id": f"dl{i}", "text": t, "is_own": 1,
                        "author_handle": "dialect_owner"}, acct)


def test_mine_detects_iraqi_with_evidence():
    _seed(*IRAQI_POSTS)
    m = dia.mine(acct=1)
    assert m["family"] == "iraqi", m["markers"]
    assert m["confidence"] > 0.6
    assert "اني " in str(m["markers"].get("iraqi", {})) or \
        m["markers"]["iraqi"], m["markers"]
    assert m["examples"], "verbatim sentences collected"


def test_mine_detects_gulf_when_gulf():
    _seed(*GULF_POSTS)
    m = dia.mine(acct=1)
    assert m["family"] == "gulf", m["markers"]
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE is_own=1")


def test_dialect_block_carries_the_profile():
    db.set_acct_setting("dialect_profile", {
        "family": "iraqi", "confidence": 0.9,
        "markers": {"اني ": 29, "هسه": 5},
        "char_words": ["كورس", "تشفير"],
        "constructions": ['"I" = اني (never أنا)'],
        "spelling_habits": ["uses مو not مش"],
        "avoid": ["Levantine هيك", "Gulf الحين"],
        "directive": "Write Iraqi only.",
        "examples": ["اني احب الهدوء هسه"],
    })
    b = dia.dialect_block(acct=1)
    assert "IRAQI" in b and "اني" in b
    assert '"I" = اني' in b and "هيك" in b  # constructions + avoid present
    assert "«اني احب الهدوء هسه»" in b  # real sentence quoted verbatim
    assert "DIALECT RULE" in b


def test_no_profile_no_block():
    db.set_acct_setting("dialect_profile", None)
    assert dia.dialect_block(acct=1) == ""


def test_voice_prompt_block_includes_dialect():
    from openstanley.gen import voice as voice_mod
    db.set_acct_setting("dialect_profile", {
        "family": "iraqi", "confidence": 0.9, "markers": {"اني ": 3},
        "constructions": ['"now" = هسه'], "directive": "Iraqi only.",
        "examples": ["هسه اشتغل"]})
    db.set_acct_setting("style_profile", None)
    vp = voice_mod.voice_prompt_block(lang="ar")
    assert "IRAQI" in vp and "هسه" in vp, "dialect rides the voice block"
    assert voice_mod.voice_prompt_block(lang="en") == "" or \
        "IRAQI" not in voice_mod.voice_prompt_block(lang="en")
    db.set_acct_setting("dialect_profile", None)


def test_build_profile_survives_llm_failure(monkeypatch):
    from openstanley.gen.llm import LLMError
    _seed(*IRAQI_POSTS)
    import openstanley.gen.llm as llm_mod
    monkeypatch.setattr(llm_mod, "chat",
                        lambda *a, **k: (_ for _ in ()).throw(LLMError("down")))
    p = dia.build_profile(Config(), acct=1)
    assert p and p["family"] == "iraqi", "deterministic half stands alone"
    assert "Iraqi" in p["directive"] or "iraqi" in p["directive"]
    with db.connect() as c:
        c.execute("DELETE FROM posts WHERE is_own=1")
