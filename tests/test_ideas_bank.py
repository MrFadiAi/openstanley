"""Idea bank replenishment v0.4.3 — the bank never runs dry.

Hermetic: per-test throwaway SQLite + sandboxed brain dir, no network (the
study-read spy records X searches instead of performing them; the API test
rides the dryrun client). No LLM — replenish is a deterministic mining chain.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"  # before importing the server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openstanley.core import db  # noqa: E402

import openstanley.server.__main__ as server  # noqa: E402  (also init_db on import)

client = TestClient(server.app)

from openstanley.core.config import Config  # noqa: E402
from openstanley.gen import brain  # noqa: E402
from openstanley.gen import drafts as drafts_mod  # noqa: E402
from openstanley.gen import ideas  # noqa: E402
from openstanley.gen.agent import Agent  # noqa: E402

STRATEGIES_MD = """# Growth Strategies

## Working theses
- Reply speed beats content volume
- Ship teardowns build trust

## Experiment log
- (none yet)
"""


class SpyX:
    """Records search calls; hands back preloaded posts. Never hits the wire."""

    mode = "spy"

    def __init__(self, results: list[dict] | None = None):
        self.searches: list[str] = []
        self._results = results or []

    async def search(self, query: str, limit: int = 50) -> list[dict]:
        self.searches.append(query)
        return self._results


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ideas.db")
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _brain_sandbox(tmp_path, monkeypatch):
    sandbox = tmp_path / "brain"
    # v0.5.0: brains live under ACCOUNTS_ROOT/<id>/brain — sandbox the anchor
    monkeypatch.setattr(brain, "ACCOUNTS_ROOT", tmp_path / "accounts")
    sandbox = brain.brain_dir()
    brain.ensure()
    yield


def _niche(x_id: str, text: str, likes: int, imp: int) -> dict:
    return {"x_id": x_id, "author_handle": f"u_{x_id}", "is_own": 0,
            "created_at": "2026-08-01T10:00:00", "text": text,
            "impressions": imp, "likes": likes, "reposts": 0, "replies": 0,
            "bookmarks": 0}


def _seed_outliers(n_topics: int = 40) -> None:
    """`n_topics` posts where the top decile are high-rate with disjoint
    topics, plus filler low-rate posts (one carrying a novel topic of its own
    that must NOT be mined)."""
    topics = [
        "quantum compilers changed everything about latency budgets",
        "gardening robots teach us unexpected lessons about patience",
        "medieval history threads outperform every tech topic this month",
        "sourdough starters are the original stateful agent",
    ][:n_topics]
    for i, txt in enumerate(topics):
        db.upsert_post(_niche(f"hi-{i}", txt, likes=50, imp=100))   # rate 0.5
    for i in range(max(1, 10 * len(topics) - len(topics))):
        txt = ("shipping notes and coffee breaks " * 3) if i else \
              "kite surf photography gear reviews nobody expects"
        db.upsert_post(_niche(f"lo-{i}", txt, likes=1, imp=100))    # rate 0.01


def _bank_titles() -> list[str]:
    with db.connect() as c:
        return [r["title"] for r in c.execute(
            "SELECT title FROM ideas WHERE status='new'").fetchall()]


# ---------------- skip when full ----------------

def test_replenish_noop_when_bank_full():
    """bank >= min → nothing mined, nothing read, ran=False."""
    for i in range(16):
        db.add_idea(f"bank filler idea {i}", "filler", "one-liner", "manual", 5)
    spy = SpyX()
    res = asyncio.run(ideas.replenish(Config(), min_bank=15, x=spy))
    assert res == {"ran": False, "added": 0, "sources": [],
                   "bank": 16, "bank_before": 16}
    assert spy.searches == []
    assert db.idea_count() == 16


# ---------------- source a: outlier mining ----------------

def test_outliers_come_from_top_decile_by_rate():
    """Only the top-10%-by-rate posts are distilled; a novel low-rate topic
    stays unmined."""
    _seed_outliers()
    res = asyncio.run(ideas.replenish(Config(), x=None))
    assert res["ran"] and res["sources"] == ["scan"]
    scan = [i for i in db.fresh_ideas(50) if i["source"] == ideas.SOURCE_SCAN]
    assert 2 <= len(scan) <= 4
    blob = " ".join((i["title"] + " " + (i["angle"] or "")) for i in scan).lower()
    assert "quantum" in blob and "sourdough" in blob          # top-rate topics mined
    assert "kite surf" not in blob                            # low-rate topic not


def test_novelty_dedupe_against_bank_any_status():
    """An outlier whose angle is already represented (token containment) is
    skipped — including when the banked idea is USED, not just fresh."""
    _seed_outliers(n_topics=1)  # one high-rate outlier: quantum…
    with db.connect() as c:     # a USED idea covering the same angle
        c.execute(
            "INSERT INTO ideas (title, angle, format, source, status, score, created_at) "
            "VALUES ('quantum compilers angle','everything about latency budgets and "
            "quantum compilers changed','one-liner','llm','used',5,'2026-07-01T00:00:00')")
    db.upsert_post(_niche("hi-extra",
                          "lighthouse keeping journals beat every productivity app",
                          likes=50, imp=100))
    res = asyncio.run(ideas.replenish(Config(), x=None))
    assert res["ran"] and res["added"] >= 1
    blob = " ".join(_bank_titles()).lower()
    assert "quantum" not in blob           # duplicate angle never re-banked
    assert "lighthouse" in blob            # the novel outlier made it in


def test_dedupe_within_batch():
    """Two near-identical outliers yield ONE idea, not two."""
    db.upsert_post(_niche("dup-1", "notebook orchestration beats every framework",
                          likes=50, imp=100))
    db.upsert_post(_niche("dup-2",
                          "notebook orchestration beats every framework for prototypes",
                          likes=45, imp=100))
    res = asyncio.run(ideas.replenish(Config(), x=None))
    scan = [i for i in db.fresh_ideas(50) if i["source"] == ideas.SOURCE_SCAN]
    assert res["added"] == len(scan) == 1


# ---------------- source b: journal distillation ----------------

def test_journal_insights_become_ideas():
    """Recent reflection bodies distill into brain-sourced ideas; placeholder
    bodies are skipped."""
    brain.journal_append("reflect:learn",
                         "Threads with contrarian hooks held 3x reply rate this week.")
    brain.journal_append("reflect:chat", "(no notes)")
    res = asyncio.run(ideas.replenish(Config(), x=None))
    brainy = [i for i in db.fresh_ideas(50) if i["source"] == ideas.SOURCE_BRAIN]
    assert res["sources"] == ["brain"]
    assert len(brainy) == 1
    assert "contrarian" in (brainy[0]["angle"] or "").lower()


# ---------------- source d: evergreen synthesis ----------------

def test_evergreen_synthesis_shape():
    """themes × strategy statements → angles that name both sides; only evergreen."""
    brain.write("strategies", STRATEGIES_MD)
    res = asyncio.run(ideas.replenish(Config(), x=None))
    assert res["sources"] == ["evergreen"]
    ev = [i for i in db.fresh_ideas(50) if i["source"] == ideas.SOURCE_EVERGREEN]
    assert len(ev) == 6  # 3 default themes × 2 thesis lines
    for i in ev:
        a = (i["angle"] or "").lower()
        assert "reply speed beats content volume" in a or \
               "ship teardowns build trust" in a
        assert any(t.lower() in a for t in Config().agent.evergreen_themes)


# ---------------- fallback chain ----------------

def test_ab_sufficient_no_x_reads():
    """Outliers alone reach batch/2 → the X client is never touched."""
    _seed_outliers()  # decile of 4 distinct high-rate topics
    spy = SpyX()
    res = asyncio.run(ideas.replenish(Config(), x=spy))
    assert spy.searches == []
    assert ideas.SOURCE_STUDY not in res["sources"]


def test_fallback_chain_order_and_study_reads():
    """a+b < batch/2 → throttled study reads fire, then evergreen tops up;
    sources appear in priority order."""
    db.upsert_post(_niche("one", "origami algorithms fold big data neatly",
                          likes=50, imp=100))
    brain.journal_append("reflect:scan",
                         "Screenshots in quotes doubled bookmark rate.")
    brain.write("strategies", STRATEGIES_MD)
    fresh = [_niche("fresh-1", "cave painting is the original design system",
                    likes=60, imp=100),
             _niche("fresh-2", "ferry schedules explain queue theory better than textbooks",
                    likes=55, imp=100),
             _niche("fresh-3", "bamboo growth curves model patience in compounding",
                    likes=52, imp=100)]
    spy = SpyX(results=fresh)
    res = asyncio.run(ideas.replenish(Config(), x=spy))

    assert res["sources"] == [ideas.SOURCE_SCAN, ideas.SOURCE_BRAIN,
                              ideas.SOURCE_STUDY, ideas.SOURCE_EVERGREEN]
    assert res["added"] == 8 and db.idea_count() == 8
    assert 1 <= len(spy.searches) <= ideas.STUDY_READ_QUERIES
    blob = " ".join(_bank_titles()).lower()
    assert "cave painting" in blob        # fresh read actually distilled


def test_study_reads_skipped_without_client():
    """No client → path (c) silently skipped, evergreen fills instead."""
    db.upsert_post(_niche("one", "origami algorithms fold big data neatly",
                          likes=50, imp=100))
    brain.journal_append("reflect:scan",
                         "Screenshots in quotes doubled bookmark rate.")
    brain.write("strategies", STRATEGIES_MD)
    res = asyncio.run(ideas.replenish(Config(), x=None))
    assert ideas.SOURCE_STUDY not in res["sources"]
    assert res["sources"] == [ideas.SOURCE_SCAN, ideas.SOURCE_BRAIN,
                              ideas.SOURCE_EVERGREEN]


# ---------------- create-loop wiring ----------------

def test_create_replenishes_before_drafting(monkeypatch):
    """Low bank → replenish runs BEFORE generate_drafts (the spy freezes the
    bank level it sees), the log line matches, and no LLM is called."""
    for i in range(14):
        db.add_idea(f"seed idea {i}", "filler", "one-liner", "manual", 5)
    _seed_outliers()
    brain.write("strategies", STRATEGIES_MD)
    seen: dict[str, int] = {}

    def _spy(cfg, count=None):
        seen["bank"] = db.idea_count()
        return []

    monkeypatch.setattr(drafts_mod, "generate_drafts", _spy)
    agent = Agent(Config())
    res = asyncio.run(agent.create())

    assert seen["bank"] >= 15, "drafting must see a replenished bank"
    assert res["drafts"] == 0 and res["bank_replenished"] >= 1
    with db.connect() as c:
        row = c.execute("SELECT message FROM agent_log WHERE loop='create' "
                        "AND message LIKE 'bank low%'").fetchone()
    assert row and row["message"].startswith("bank low (14) — replenished +")


# ---------------- API + persistence ----------------

def test_replenish_endpoint_and_bank_health():
    """POST /api/ideas/replenish adds ideas; GET /api/ideas/bank reports the
    count and the last replenish record."""
    _seed_outliers()
    brain.write("strategies", STRATEGIES_MD)
    r = client.post("/api/ideas/replenish").json()
    assert r["ran"] is True and r["added"] >= 2
    assert set(r["sources"]) <= {ideas.SOURCE_SCAN, ideas.SOURCE_BRAIN,
                                 ideas.SOURCE_STUDY, ideas.SOURCE_EVERGREEN}

    health = client.get("/api/ideas/bank").json()
    assert health["count"] == db.idea_count() == r["bank"]
    assert health["last"]["added"] == r["added"]
    assert health["last"]["sources"] == r["sources"]
    assert health["last"]["at"]

    listed = client.get("/api/ideas").json()
    assert listed and all(i["source"] in (
        ideas.SOURCE_SCAN, ideas.SOURCE_BRAIN, ideas.SOURCE_STUDY,
        ideas.SOURCE_EVERGREEN) for i in listed)


def test_idea_source_persisted_for_analytics():
    """Every replenished row carries its source badge; the last-replenish
    setting keeps sources in priority order."""
    db.upsert_post(_niche("one", "origami algorithms fold big data neatly",
                          likes=50, imp=100))
    brain.journal_append("reflect:scan",
                         "Screenshots in quotes doubled bookmark rate.")
    brain.write("strategies", STRATEGIES_MD)
    res = asyncio.run(ideas.replenish(Config(), x=SpyX(results=[
        _niche("fresh-1", "cave painting is the original design system",
               likes=60, imp=100)])))
    with db.connect() as c:
        rows = c.execute("SELECT source FROM ideas").fetchall()
    assert {r["source"] for r in rows} == {"scan", "brain", "study", "evergreen"}
    last = db.get_setting("ideas_last_replenish")
    assert last["sources"] == res["sources"] == \
        ["scan", "brain", "study", "evergreen"]
    assert last["at"] and last["added"] == res["added"]
