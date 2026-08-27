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
