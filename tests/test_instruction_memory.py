"""Instruction memory — chat directives become standing brain rules."""
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
from openstanley.gen import brain as brain_mod                     # noqa: E402
from openstanley.gen import instructions as im                     # noqa: E402
from openstanley.gen import tools as tools_mod                     # noqa: E402

CFG = Config()

_DIRECTIVE_JSON = json.dumps({
    "is_directive": True,
    "text": "Never use hashtags in posts",
    "scope": "posts",
})
_NOT_DIRECTIVE_JSON = json.dumps({"is_directive": False})


def _active() -> list[dict]:
    return [r for r in brain_mod.parse_rules(brain_mod.read("rules", 1))
            if r["status"] == "active"]


def test_gate_catches_directives_en_ar_and_passes_ordinary():
    assert im.looks_like_directive("never end posts with questions")
    assert im.looks_like_directive("Always write in Arabic for politics")
    assert im.looks_like_directive("rule: no threads on weekends")
    assert im.looks_like_directive("لا تنشر عن السياسة أبدا")
    assert im.looks_like_directive("من الأفضل أن تتوقف عن الهاشتاغات")
    assert not im.looks_like_directive("what is my best post this week?")
    assert not im.looks_like_directive("write me a post about coffee")
    assert not im.looks_like_directive("")


def test_capture_skips_llm_entirely_when_gate_fails(monkeypatch):
    def _boom(*a, **k):  # any LLM call here = a bug
        raise AssertionError("LLM called for a non-directive message")
    monkeypatch.setattr(im, "llm_chat", _boom)
    assert im.capture(CFG, "what is my best post this week?") is None


def test_capture_stores_directive_rule(monkeypatch):
    monkeypatch.setattr(im, "llm_chat", lambda *a, **k: _DIRECTIVE_JSON)
    before = _active()
    res = im.capture(CFG, "can you please stop with the hashtags")
    assert res and res["rule_id"] > 0
    rules = _active()
    new = [r for r in rules if r["id"] not in {b["id"] for b in before}]
    assert len(new) == 1 and new[0]["source"] == "directive"
    assert new[0]["text"] == "Never use hashtags in posts"
    # journal records the capture with WHY
    assert "directive" in brain_mod.read("journal", 1)
    # the ack line the user sees carries the rule id
    assert f"R{res['rule_id']}" in im.ack_line(res)


def test_capture_llm_says_not_a_directive(monkeypatch):
    monkeypatch.setattr(im, "llm_chat", lambda *a, **k: _NOT_DIRECTIVE_JSON)
    before = len(_active())
    assert im.capture(CFG, "never mind, just show me analytics") is None
    assert len(_active()) == before


def test_capture_never_breaks_on_llm_error(monkeypatch):
    from openstanley.gen.llm import LLMError
    monkeypatch.setattr(im, "llm_chat",
                        lambda *a, **k: (_ for _ in ()).throw(LLMError("down")))
    assert im.capture(CFG, "never use hashtags") is None


def test_add_directive_dedupes_restatements():
    rid = im.add_directive("Never schedule posts after midnight", acct=1)
    assert rid > 0
    again = im.add_directive("never schedule posts AFTER midnight", acct=1)
    assert again == -rid, "a restatement maps to the existing rule"
    assert len([r for r in _active() if r["source"] == "directive"]) == 1
    different = im.add_directive(
        "Replies should quote the specific point they answer", acct=1)
    assert different > 0


def test_brain_context_hoists_directives_above_learned_rules():
    brain_mod.add_rule("Post data-driven threads on Sundays", source="learn",
                       acct=1)
    im.add_directive("Never use hashtags in posts", acct=1)
    ctx = brain_mod.brain_context(acct=1)
    assert "OWNER DIRECTIVES" in ctx
    assert "Never use hashtags in posts" in ctx
    assert ctx.index("OWNER DIRECTIVES") < ctx.index("RULES (learned"), \
        "owner law precedes learned heuristics in the prompt"


def test_remember_rule_tool_persists():
    before = len(_active())
    res = tools_mod.execute_tool(CFG, "remember_rule",
                                 {"text": "My audience is mostly Saudi builders"})
    assert res["ok"] and res.get("rule_id")
    assert len(_active()) == before + 1
    new = [r for r in _active() if r["source"] == "directive"]
    assert any("Saudi builders" in r["text"] for r in new)


def test_remember_rule_tool_requires_text():
    res = tools_mod.execute_tool(CFG, "remember_rule", {})
    assert not res["ok"]


def test_secret_shaped_directive_is_refused():
    # the brain's own sanitize runs inside add_rule — a secret-like rule
    # must raise, never persist
    try:
        im.add_directive("api_key = sk-1234567890abcdefghijklmnop", acct=1)
        stored = [r for r in _active()
                  if "api_key" in r["text"].lower()]
        assert not stored, "secret-shaped directive was refused"
    except brain_mod.BrainSecurityError:
        pass  # the correct outcome
