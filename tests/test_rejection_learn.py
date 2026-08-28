"""Rejection learning loop — the owner's NO becomes brain rules."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import brain as brain_mod                     # noqa: E402
from openstanley.gen import rejection_learn as rl                  # noqa: E402

CFG = Config()

_FAKE_REFLECT_JSON = json.dumps({
    "instructions_delta": "",
    "new_rules": [{"text": "Never draft engagement-bait question threads",
                   "why": "owner rejected three of them"}],
    "retire_rule_ids": [],
    "strategy_updates": [],
    "file_updates": [],
    "journal_entry": "owner rejects question-bait threads",
})


def _mk_rejected(text: str, reason: str = "owner", via: str = "web") -> int:
    did = db.add_draft(text=text, kind="post", acct=1,
                       meta={"source": "create"})
    db.update_draft(did, acct=1, status="rejected")
    rl.record_rejection(did, reason=reason, via=via, acct=1)
    return did


def _isolate() -> None:
    """Other suites' TG-reject tests stamp owner-rejections into the shared
    DB — pending_owner_rejections() would count them here (live: a 3rd
    pending rejection broke the ==2 expectation). Clear the slate first."""
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE status='rejected'")


def _cleanup(*ids: int) -> None:
    with db.connect() as c:
        for i in ids:
            c.execute("DELETE FROM drafts WHERE id=?", (i,))


def test_record_rejection_stamps_meta():
    did = _mk_rejected("hashtag soup about ai #ai #tech")
    d = db.get_draft(did, acct=1)
    assert d["meta"]["rejected_reason"] == "owner"
    assert d["meta"]["rejected_via"] == "web"
    assert d["meta"]["rejected_at"]
    _cleanup(did)


def test_record_rejection_idempotent_and_only_on_rejected():
    did = _mk_rejected("one", via="web")
    rl.record_rejection(did, reason="expired", via="sweep", acct=1)
    d = db.get_draft(did, acct=1)
    assert d["meta"]["rejected_reason"] == "owner", "first reason wins"
    d2 = db.add_draft(text="still pending", acct=1)
    rl.record_rejection(d2, reason="owner", via="web", acct=1)
    assert not (db.get_draft(d2, acct=1)["meta"] or {}).get("rejected_reason")
    _cleanup(did, d2)


def test_pending_excludes_expired_and_learned():
    _isolate()
    e = _mk_rejected("expired thing", reason="expired", via="sweep")
    o1 = _mk_rejected("owner hates this")
    o2 = _mk_rejected("owner hates this too")
    pending = [d["id"] for d in rl.pending_owner_rejections(acct=1)]
    assert o1 in pending and o2 in pending and e not in pending
    # learned flag removes it from the pending set
    d = db.get_draft(o1, acct=1)
    rl._mark_learned([o1], acct=1)
    assert o1 not in [x["id"] for x in rl.pending_owner_rejections(acct=1)]
    _cleanup(e, o1, o2)


def test_build_material_contrasts_rejected_vs_approved():
    _isolate()
    o = _mk_rejected("bait thread ending in a question?")
    a = db.add_draft(text="calm observation the owner approved", acct=1)
    db.update_draft(a, acct=1, status="approved")
    material = rl.build_material(acct=1)
    assert "bait thread ending in a question?" in material
    assert "calm observation the owner approved" in material
    assert "REJECTED BY THE OWNER" in material
    assert "contrast" in material
    _cleanup(o, a)


def test_run_reflection_adds_rules_and_marks_learned(monkeypatch):
    _isolate()
    monkeypatch.setattr(brain_mod, "llm_chat",
                        lambda *a, **k: _FAKE_REFLECT_JSON)
    before = {r["id"] for r in brain_mod.parse_rules(
        brain_mod.read("rules", 1))}
    o1 = _mk_rejected("question bait one?")
    o2 = _mk_rejected("question bait two?")
    res = rl.run_reflection(CFG, acct=1)
    assert res["ok"] and res["learned_from"] == 2
    rules = brain_mod.parse_rules(brain_mod.read("rules", 1))
    new = [r for r in rules if r["id"] not in before]
    assert any(r["source"] == "rejection" for r in new), \
        "rules land with source=rejection"
    for did in (o1, o2):
        assert db.get_draft(did, acct=1)["meta"].get("rejection_learned")
    assert rl.pending_owner_rejections(acct=1) == [] or all(
        d["id"] not in (o1, o2) for d in rl.pending_owner_rejections(acct=1))
    # journal recorded the pass
    assert "reject" in brain_mod.read("journal", 1).lower()
    _cleanup(o1, o2)


def test_run_reflection_noop_when_nothing_pending():
    res = rl.run_reflection(CFG, acct=1)
    assert res["ok"] and res["learned_from"] == 0


def test_maybe_reflect_async_threshold(monkeypatch):
    _isolate()
    fired = []
    monkeypatch.setattr(rl, "run_reflection",
                        lambda cfg, acct=None: fired.append(1) or
                        {"ok": True, "learned_from": 1})
    o1 = _mk_rejected("one?")
    o2 = _mk_rejected("two?")
    assert rl.maybe_reflect_async(CFG, acct=1) is False, "2 < threshold 3"
    o3 = _mk_rejected("three?")
    assert rl.maybe_reflect_async(CFG, acct=1) is True
    for _ in range(50):  # daemon thread lands quickly; bounded wait
        if fired:
            break
        time.sleep(0.05)
    assert fired, "reflection fired in the background"
    _cleanup(o1, o2, o3)


def test_expired_never_reaches_the_llm(monkeypatch):
    called = []
    monkeypatch.setattr(brain_mod, "llm_chat",
                        lambda *a, **k: called.append(1) or _FAKE_REFLECT_JSON)
    e1 = _mk_rejected("old one", reason="expired", via="sweep")
    e2 = _mk_rejected("old two", reason="expired", via="sweep")
    e3 = _mk_rejected("old three", reason="expired", via="sweep")
    e4 = _mk_rejected("old four", reason="expired", via="sweep")
    res = rl.run_reflection(CFG, acct=1)
    assert res["learned_from"] == 0 and called == [], \
        "expiry rejections are hygiene — never learned as taste"
    _cleanup(e1, e2, e3, e4)


def test_consolidation_retires_near_duplicates():
    """Live 2026-08-28: 18 rules from 10 rejections — the same 3 lessons
    repeated 5+ times because reflect emitted one rule per draft. The
    consolidation keeps the earliest of each near-duplicate cluster."""
    from openstanley.gen import rejection_learn as rl
    a = rl._consolidate_rules.__self__ if hasattr(rl._consolidate_rules, "__self__") else None
    r1 = brain_mod.add_rule("DON'T write replies that warn, scold, or "
                            "lecture about twitter culture", "rejection", acct=1)
    r2 = brain_mod.add_rule("DON'T write replies that warn, scold, lecture, "
                            "or analyze twitter culture/people",
                            "rejection", acct=1)
    r3 = brain_mod.add_rule("DO open niche replies with specific praise "
                            "and a concrete observation", "rejection", acct=1)
    n = rl._consolidate_rules(CFG, acct=1)
    assert n >= 1, "the near-duplicate pair should collapse"
    rules = {r["id"]: r for r in brain_mod.parse_rules(
        brain_mod.read("rules", 1))}
    assert rules[r2]["status"] == "retired", "later duplicate retired"
    assert rules[r1]["status"] == "active", "earliest kept"
    assert rules[r3]["status"] == "active", "distinct rule untouched"


def test_llm_merge_clusters_paraphrases(monkeypatch):
    """The live 18-rule set is PARAPHRASE duplicates — token overlap caught
    0 of them. The LLM merge proposes clusters; only valid, complete
    proposals apply."""
    import json as _json
    import openstanley.gen.llm as llm_mod
    praise_ids = [brain_mod.add_rule(
        f"praise variant {i}: replies must open with specific praise, not "
        f"generic agreement templates (form {i})", "rejection", acct=1)
        for i in range(3)]
    meta_ids = [brain_mod.add_rule(
        f"meta variant {i}: do not meta-comment about twitter culture "
        f"(form {i})", "rejection", acct=1) for i in range(2)]
    proposal = _json.dumps({"clusters": [
        {"keep": praise_ids[0], "retire": praise_ids[1:]},
        {"keep": meta_ids[0], "retire": [meta_ids[1]]}],
        "solo": []})
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: proposal)
    n = rl._consolidate_rules(CFG, acct=1)
    assert n == 3, "4 paraphrase dupes retire into 2 keeps"
    rules = {r["id"]: r for r in brain_mod.parse_rules(
        brain_mod.read("rules", 1))}
    assert rules[praise_ids[0]]["status"] == "active"
    assert rules[praise_ids[2]]["status"] == "retired"


def test_llm_merge_rejects_bad_proposal_nothing_retired(monkeypatch):
    """A proposal naming an UNKNOWN id is rejected whole — the token-pass
    fallback runs instead and no rule is lost to a hallucinated merge."""
    import json as _json
    import openstanley.gen.llm as llm_mod
    r1 = brain_mod.add_rule("lesson one text here", "rejection", acct=1)
    r2 = brain_mod.add_rule("a completely different lesson two",
                            "rejection", acct=1)
    bad = _json.dumps({"clusters": [{"keep": 99999, "retire": [r2]}],
                       "solo": []})
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: bad)
    rl._consolidate_rules(CFG, acct=1)
    rules = {r["id"]: r for r in brain_mod.parse_rules(
        brain_mod.read("rules", 1))}
    assert rules[r1]["status"] == "active"
    assert rules[r2]["status"] == "active", "bad proposal retires nothing"
