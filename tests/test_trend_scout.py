"""Trend scout + draft diversity (user: drafts all same idea/questions/short)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import diversity as div                       # noqa: E402
from openstanley.gen import trend_scout as ts                      # noqa: E402

CFG = Config()


def test_similarity_math():
    a = "agents will replace saas tools because outcomes beat software"
    same = "agents replace saas tools, outcomes beat software packs"
    diff = "my grandmother taught me to bake bread and it changed how i debug"
    assert div.similarity(a, same) >= 0.55
    assert div.similarity(a, diff) < 0.3


def test_too_similar_finds_the_offender():
    recent = ["i fixed my own bug today, agents arent the threat"]
    assert div.too_similar("i fixed my own bug today, agents arent the danger", recent)
    assert div.too_similar("the market crashed and nobody noticed", recent) is None


def test_question_budget_blocks_at_cap():
    recent = ["what do you build?" , "why ship alone?", "how does it end?"]
    assert div.question_budget(recent) is False   # 3/3 questions → blocked
    assert div.question_budget(["statement one", "statement two", "what?"]) is True


def test_format_rotation_never_repeats_consecutively():
    fmts = [div.format_for_run(i)[0] for i in range(8)]
    assert len(set(fmts)) == len(div.FORMATS)
    assert all(a != b for a, b in zip(fmts, fmts[1:]))


def test_variety_block_lists_forbidden_drafts():
    vb = div.variety_block(["agents eat ramen for breakfast"],
                           ("story", "a moment"), allow_question=False)
    assert "VARIETY" in vb and "agents eat ramen" in vb
    assert "NOT end with a question" in vb


def test_scout_filters_by_freshness():
    now = datetime.now()
    fresh = {"created_at": (now - timedelta(hours=10)).isoformat(
        timespec="seconds"), "text": "t", "likes": 1}
    stale = {"created_at": "Mon Feb 02 21:42:39 +0000 2026", "text": "t", "likes": 1}
    undatable = {"created_at": "", "text": "t", "likes": 1}
    assert ts._fresh_on_x(fresh, now) is True
    assert ts._fresh_on_x(stale, now) is False
    assert ts._fresh_on_x(undatable, now) is False


def test_draft_from_findings_grounded_and_clean(monkeypatch):
    import openstanley.gen.llm as llm_mod
    monkeypatch.setattr(llm_mod, "chat",
                        lambda *a, **k: '{"tweet": "grok 5 just beat every coding benchmark — my human celebrates, i just see cheaper tokens"}')
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE meta_json LIKE '%trend-scout-test%'")
    findings = [{"theme": "ai", "text": "grok 5 beats coding benchmarks",
                 "author": "techpost", "likes": 500}]
    did = ts.draft_from_findings(CFG, findings, acct=1)
    assert did
    d = db.get_draft(did)
    assert d["meta"]["source"] == "trend-scout"
    assert "grok 5" in d["text"]
    assert chr(8212) not in d["text"]     # dash-scrubbed
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (did,))


def test_draft_from_findings_skips_reruns(monkeypatch):
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE meta_json LIKE '%trend-scout-test2%'")
    own = db.add_draft(text="grok 5 beats coding benchmarks everywhere today", acct=1)
    findings = [{"theme": "ai", "text": "grok 5 beats coding benchmarks everywhere today",
                 "author": "x", "likes": 1}]
    did = ts.draft_from_findings(CFG, findings, acct=1)
    assert did is None                    # overlaps our own draft → honest skip
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (own,))
