"""X Strategy one-pager — mirrors OpenStanley's "My Human / X Strategy" screen.

Generates: Strategic Goal, Target Audience, Positioning Line, Content Pillars,
Core Message — from the user's real posts, voice, and niche data.
"""
from __future__ import annotations

import json

from ..core import db
from ..core.config import Config
from . import brain as brain_mod
from .llm import chat as llm_chat, LLMConfig, LLMError

PROMPT = """You are OpenStanley, an AI Head of Content. Based ONLY on the account data
below (real posts, real numbers, learned rules, what actually performed),
write a one-page X content strategy in EXACTLY this structure (plain text,
no markdown headers — the section title alone on its line):

Strategic Goal
<one paragraph: grow this account over 6-12 months by turning its actual
proof/lessons/judgment into a consistent content engine. If the follower
count is in the data, set a concrete follower target with a date. No fluff.>

Target Audience
Primary: <who this account already serves — inferred from who engages and
what the top posts talk about>
Secondary: <adjacent audience the data supports>
Others: <reach-expanding audience, only if the data supports it>

Positioning Line
<one line, first person: "I'm the person you follow if you want..." —
specific to THIS account's actual content, no generic thought-leader talk>

Content Pillars
<3-5 pillars derived from what this account ACTUALLY posts and what
performed. EACH pillar in exactly this shape (percentages are the posting
mix and MUST sum to 100, weighted by real engagement evidence):

N% Pillar name
Core Message: <the single belief this pillar repeats>
What I Share:
- <concrete content type from the account's real posts>
- <...>
Why It Matters: <one line on why THIS audience cares>

Posting Rhythm
Cadence: <posts per week the account's real history supports>
Default Windows: <the actual best hours from the data, with timezone>
Schedule Patterns: <consistency rules derived from real posting history>

Notes
What I Lean Into:
- <3-5 bullets: structures/hooks/proof types the engagement data rewards>
What I Avoid:
- <3-6 bullets GROUNDED IN THE LEARNED RULES — real rejection patterns,
  voice violations, anything the account's history says fails>

Every claim must trace to the data below. Never invent audience segments,
numbers, or proof this account doesn't have.

=== ACCOUNT DATA ===
{data}
"""


def _account_data() -> str:
    me = db.get_me()
    parts = [f"@{me.get('username','?')} — {me.get('followers','?')} followers"]
    own = db.own_posts(40)
    if own:
        parts.append("RECENT POSTS (newest first):")
        for p in own[:40]:
            parts.append(f"- [{p['likes']}♥ {p['replies']}💬] {p['text'][:140]}")
    vp = db.get_setting("voice_profile")
    if vp:
        parts.append("VOICE: " + str(vp.get("rubric", ""))[:400])
        mix = (vp.get("stats") or {}).get("language_mix")
        if mix:
            parts.append(f"LANGUAGE MIX: {json.dumps(mix)}")
    niche = db.niche_posts(30)
    if niche:
        tops = sorted(niche, key=lambda p: p.get("engagement", 0), reverse=True)[:15]
        parts.append("NICHE TOP PERFORMERS:")
        for p in tops:
            parts.append(f"- [{p['likes']}♥ {p['replies']}💬] {p['text'][:120]}")
    # real performance evidence: best hours from the style profile
    sp = db.get_acct_setting("style_profile") or {}
    bh = ((sp.get("stats") or {}).get("posting_times") or {}).get("best_hours")
    if bh:
        parts.append(f"REAL BEST POSTING HOURS: {bh}")
    # learned what-works (theses) and what-fails (active rules) — the
    # Avoid list and the pillar weights come from these
    try:
        strat = brain_mod.read("strategies")
        theses = strat.split("## Experiment log")[0]
        theses = "\n".join(ln for ln in theses.splitlines()
                           if ln.strip().startswith("-"))
        if theses:
            parts.append("WORKING THESES (learned from real performance):\n" + theses)
    except FileNotFoundError:
        pass
    try:
        rules_txt = brain_mod.read("rules")
        active = [ln for ln in rules_txt.splitlines()
                  if ln.strip().startswith("- R") and "retired" not in ln]
        if active:
            parts.append("LEARNED RULES (owner-confirmed DO/DON'Ts — obey in "
                         "'What I Avoid'):\n" + "\n".join(active[:20]))
    except FileNotFoundError:
        pass
    # brain reference docs: current pillars + personas feed the refinement
    for stem, label in (("content-pillars", "CURRENT CONTENT PILLARS (brain)"),
                        ("audience-personas", "CURRENT AUDIENCE PERSONAS (brain)")):
        try:
            text = brain_mod.read(f"files/{stem}")
        except FileNotFoundError:
            continue
        real = [ln for ln in text.splitlines()
                if ln.strip() and not ln.startswith("#")
                and "(OpenStanley writes" not in ln]
        if real:
            parts.append(f"{label}:\n" + "\n".join(real[:20]))
    return "\n".join(parts)


def build_strategy(cfg: Config, force: bool = False) -> dict:
    """Generate + persist the strategy one-pager. Returns the strategy dict."""
    existing = db.get_acct_setting("strategy")
    if existing and not force:
        return existing
    import dataclasses
    # 1800 starved GLM's thinking phase — empty reply, RuntimeError, HTTP
    # 500, and the Strategy page showed nothing after 30-40s of waiting
    # (live 2026-09-01: the same starvation class fixed across chat/TG)
    llm_cfg = dataclasses.replace(cfg.llm, temperature=0.6,
                                  max_tokens=max(cfg.llm.max_tokens, 4000))
    prompt = PROMPT.replace("{data}", _account_data())
    try:
        text = llm_chat(llm_cfg, system="You are OpenStanley, an AI Head of Content.", user=prompt)
    except LLMError as e:
        raise RuntimeError(f"Strategy generation failed: {e}") from e
    strategy = {
        "text": text,
        "sections": _parse_sections(text),
        "generated_at": db.__dict__.get("_now", lambda: "")() or None,
    }
    _sync_brain_docs(strategy["sections"])
    db.set_acct_setting("strategy", strategy)
    db.log("system", "strategy one-pager generated")
    return strategy


def _sync_brain_docs(sections: dict) -> None:
    """Write the refined pillars/personas back into the brain files."""
    if not sections:
        return
    for section, stem in (("Content Pillars", "content-pillars"),
                          ("Target Audience", "audience-personas")):
        body = (sections.get(section) or "").strip()
        if not body:
            continue
        try:
            brain_mod.write(f"files/{stem}",
                            f"# {section} (refined {db._now()[:10]})\n\n{body}\n")
        except brain_mod.BrainSecurityError as e:
            db.log("system", f"brain sync for {stem} skipped: {e}", level="warn")


def _parse_sections(text: str) -> dict:
    """Split the one-pager into sections by known headers."""
    keys = ["Strategic Goal", "Target Audience", "Positioning Line",
            "Content Pillars", "Core Message", "Voice & Style",
            "Cadence & Formats", "Posting Rhythm", "Notes"]
    sections = {}
    current = None
    for line in text.splitlines():
        stripped = line.strip().rstrip(":")
        matched = next((k for k in keys if stripped.lower() == k.lower()), None)
        if matched:
            current = matched
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if v}
