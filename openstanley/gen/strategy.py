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

PROMPT = """You are OpenStanley, an AI Head of Content. Based on the account data below,
write a one-page X content strategy. Follow EXACTLY this structure (plain text):

Strategic Goal
<one paragraph: grow the account 6-12 months by turning their unique proof/lessons
into a consistent content engine; include a concrete-feeling trajectory, not fluff>

Target Audience
<2-3 short paragraphs naming WHO this account serves and what those people care about>

Positioning Line
<one line, first person: "Follow me if you want to see..." — specific, no generic thought-leader talk>

Content Pillars
<3-5 pillars as "Name: one-sentence description" — derived from what they actually post about>

Core Message
<the single belief their content repeats, one paragraph>

Voice & Style
<3-5 bullet rules distilled from their actual posts>

Cadence & Formats
<recommended posting rhythm + formats that fit their voice>

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
    niche = db.niche_posts(30)
    if niche:
        tops = sorted(niche, key=lambda p: p.get("engagement", 0), reverse=True)[:15]
        parts.append("NICHE TOP PERFORMERS:")
        for p in tops:
            parts.append(f"- [{p['likes']}♥ {p['replies']}💬] {p['text'][:120]}")
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
    existing = db.get_setting("strategy")
    if existing and not force:
        return existing
    import dataclasses
    llm_cfg = dataclasses.replace(cfg.llm, temperature=0.6, max_tokens=1800)
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
    db.set_setting("strategy", strategy)
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
            "Content Pillars", "Core Message", "Voice & Style", "Cadence & Formats"]
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
