"""Precision mode — fewer, better drafts (owner data: 16 published vs 57
rejected in one week = 22% approval; the create loop's volume was wasting
the owner's attention).

Before the create loop saves any draft, the pre-flight filter:
1. scores it against ACTIVE rejection rules (the learned DON'Ts) — a
   strong match means the owner already rejected this pattern; skip it
2. checks the diversity/sameness gate
3. hard caps the batch at PRECISION_MAX per run

The result: at most 2 cards per create run, each pre-vetted against
everything the owner has ever rejected.
"""
from __future__ import annotations

import re
from typing import Optional

from ..core import db

PRECISION_MAX = 2          # hard cap per create run in precision mode


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[\w؀-ۿ]+", text)}


def rule_violation_score(text: str, rules: list[dict]) -> tuple[int, Optional[dict]]:
    """How strongly this text matches a learned rejection rule.
    Returns (score 0-100, matching rule). Rules are DON'Ts learned from
    the owner's actual rejections — a strong token match means the owner
    already refused this pattern."""
    text_toks = _tokens(text)
    if not text_toks or not rules:
        return 0, None
    best, best_rule = 0, None
    for r in rules:
        rule_toks = _tokens(r["text"])
        if not rule_toks:
            continue
        overlap = len(text_toks & rule_toks) / max(len(rule_toks), 1)
        # a draft matching >=60% of a rejection rule's tokens is very
        # likely the same pattern the owner already refused
        score = int(overlap * 100)
        if score > best:
            best, best_rule = score, r
    return best, best_rule


def preflight(text: str, acct: Optional[int] = None,
              recent_drafts: Optional[list[str]] = None) -> tuple[bool, str]:
    """Should this draft be saved? (ok, reason). Checks rejection-rule
    match + sameness. Pure function of text + brain state."""
    from . import brain as brain_mod
    rules = [r for r in brain_mod.parse_rules(brain_mod.read("rules", acct))
             if r["status"] == "active" and r["source"] in
             ("rejection", "chat", "learn")]
    negative_prefixes = ("DON'T", "DO NOT", "NEVER", "لا ", "نمنع")
    donts = [r for r in rules
             if r["text"].strip().upper().startswith(negative_prefixes)]
    score, hit = rule_violation_score(text, donts)
    if score >= 60 and hit:
        return False, (f"matches rejection rule R{hit['id']} at {score}% "
                      f"(owner already refused this pattern)")
    if recent_drafts:
        toks = _tokens(text)
        for prev in recent_drafts:
            other = _tokens(prev)
            if toks and other and len(toks & other) / max(len(toks | other), 1) >= 0.6:
                return False, "too similar to a recent draft"
    return True, ""
