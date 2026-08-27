"""Instruction memory — the owner's standing directives, captured from chat.

"Never end posts with questions." "No more AI news." "دايما اكتب بالعربي
للمواضيع السياسية." Today these live only in chat history and might reach
the brain if the every-10th-message reflect("chat") happens to catch them
diluted among 25 messages of material. This module captures them at the
moment they're said, on both surfaces (web + Telegram chat).

Two-stage capture keeps the cost near zero:
  1. a deterministic EN+AR gate — ordinary messages never spend an LLM call;
  2. one small confirmation call that also NORMALIZES the phrasing into a
     <=140-char imperative rule ("can you stop with the hashtags please" →
     "Never use hashtags in posts").

Directives are stored as brain rules with source="directive" — one store:
they appear in the Brain inventory, retire through the existing rule
retirement, and brain_context() hoists them into their own OWNER DIRECTIVES
block ahead of learned rules.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ..core import db
from ..core.config import Config
from .llm import chat as llm_chat, extract_json, LLMError

# deterministic gate — imperative/directive-shaped openings, EN + AR.
# Must catch "always", "never", "stop", "no more", "from now on", explicit
# "rule:"/"instruction:", and the Arabic equivalents. Deliberately wide:
# stage 2 filters. ("rule:" needs its own alternative — no \b after a colon.)
DIRECTIVE_RE = re.compile(
    r"(?i)\b(?:always|never|stop|don'?t|do not|no more|from now on|"
    r"avoid|make sure|remember to)\b"
    r"|(?:^|\W)(rule|instruction)\s*:"
    r"|\b(دايما|دائما|أبدا|ابدا|لا تنشر|لا تكتب|لا تستخدم|من الأفضل|توقف|بلا|منع|قاعدة)"
)

# token-overlap threshold: a new directive this similar to an active one is
# the same law restated, not new law
DEDUPE_OVERLAP = 0.6

CONFIRM_SYSTEM = """You decide whether a chat message is the user giving a
STANDING DIRECTIVE about how to run their X account (topics, tone, format,
language, schedule, engagement behavior) — something that should govern
FUTURE work, not a one-off request for one post.

Reply STRICT JSON:
{"is_directive": true|false,
 "text": "the directive as a short imperative rule, <=140 chars, same
          language as the user, no quotes",
 "scope": "posts|replies|all"}

false for questions, one-shot post requests ("write about X now"),
analytics asks, and small talk."""


def looks_like_directive(message: str) -> bool:
    """Cheap deterministic gate — stage 1 of the capture."""
    return bool(DIRECTIVE_RE.search(message or ""))


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[\w؀-ۿ]+", text)}


def _duplicate_of(text: str, directives: list[dict]) -> Optional[int]:
    toks = _tokens(text)
    for d in directives:
        other = _tokens(d["text"])
        if toks and other and len(toks & other) / max(len(toks | other), 1) >= DEDUPE_OVERLAP:
            return int(d["id"])
    return None


def active_directives(acct: Optional[int] = None) -> list[dict]:
    from . import brain as brain_mod
    return [r for r in brain_mod.parse_rules(brain_mod.read("rules", acct))
            if r["status"] == "active" and r["source"] == "directive"]


def add_directive(text: str, acct: Optional[int] = None) -> Optional[int]:
    """Persist a standing directive as a directive-sourced brain rule.
    Deduped — a restatement returns the existing rule id untouched."""
    from . import brain as brain_mod
    text = text.strip()
    if not text:
        return None
    dup = _duplicate_of(text, active_directives(acct))
    if dup is not None:
        return -dup  # negative = already known (caller can say "still in force")
    rid = brain_mod.add_rule(text, source="directive", acct=acct)
    brain_mod.journal_append(
        "directive", f"Owner directive captured from chat: {text[:160]}",
        [f"added R{rid} (source=directive)"], acct=acct)
    return rid


def capture(cfg: Config, user_message: str,
            acct: Optional[int] = None) -> Optional[dict]:
    """Full capture pipeline for one user message. Returns
    {"rule_id", "text"} when a NEW directive was stored, {"rule_id": -id,
    "text"} when it restated a known one, None otherwise (or on any failure
    — instruction capture must never break chat)."""
    if not looks_like_directive(user_message):
        return None
    try:
        raw = llm_chat(cfg.llm, system=CONFIRM_SYSTEM,
                       user=user_message[:800], temperature=0.0,
                       json_mode=True)
        data = extract_json(raw)
        if not isinstance(data, dict) or not data.get("is_directive"):
            return None
        text = str(data.get("text") or "").strip()
        if not text:
            return None
        rid = add_directive(text, acct)
        if rid is None:
            return None
        return {"rule_id": rid, "text": text}
    except (LLMError, ValueError) as e:  # noqa: BLE001 — best-effort capture
        db.log("brain", f"directive capture skipped: {e}", level="warn")
        return None


def ack_line(result: dict) -> str:
    """The visible confirmation appended to the chat reply — the owner SEES
    the memory land, with the rule id they can retire later."""
    rid = result["rule_id"]
    if rid < 0:
        return f"🧠 Still standing rule R{-rid} — already in force."
    return (f"🧠 Noted as standing rule R{rid} — I'll follow it from now on. "
            "(Brain → rules to retire it)")


def _tool_remember_rule(cfg: Config, text: str = "") -> dict:
    """Chat tool: the model persists a rule on explicit request — the
    reliable path when the user says 'remember this' about account facts
    the regex gate might miss (audience, context, preferences)."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "text required"}
    rid = add_directive(text)
    if rid is None:
        return {"ok": False, "error": "empty rule"}
    if rid < 0:
        return {"ok": True, "rule_id": -rid, "duplicate": True,
                "note": f"already standing rule R{-rid}"}
    return {"ok": True, "rule_id": rid, "note": f"stored as R{rid}"}
