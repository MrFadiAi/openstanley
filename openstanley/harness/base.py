"""Shared harness plumbing: eval context + hermetic draft generation.

Suites never call the product's `drafts.generate_drafts` (that would consume
real ideas and fill the user's approval queue). Instead they build the SAME
prompt the product builds — voice rubric + algorithm block + optional brain
context — and call the eval LLM directly. Fake mode is fully deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..core.config import Config
from ..gen import brain as brain_mod
from ..gen.algorithm import PROMPT_BLOCK as ALGO_PROMPT_BLOCK
from ..gen.drafts import DRAFT_SYSTEM
from ..gen.llm import extract_json
from ..gen.voice import voice_prompt_block
from ..gen.lang import draft_language_instruction

# LLM callable with the same signature as gen.llm.chat
LLMFn = Callable[..., str]


@dataclass
class EvalContext:
    """Everything a suite needs. `llm` is fake_chat in fake mode, llm.chat in real."""
    cfg: Config
    llm: LLMFn
    real: bool
    n: int                      # sample count (cfg.harness.sample_count)
    use_brain: bool             # False in the A/B no-brain arm
    run_id: int
    label: str
    notes: list[str] = field(default_factory=list)

    def brain_prefix(self) -> str:
        """OpenStanley's brain digest — empty in the no-brain A/B arm."""
        return brain_mod.brain_context() + "\n\n" if self.use_brain else ""


# fixed eval ideas — stable across runs so scores are comparable
EVAL_IDEAS = [
    {"title": "lesson from shipping the ugly version",
     "angle": "the first version teaches you what the polished one never will",
     "format": "one-liner"},
    {"title": "contrarian take on tools",
     "angle": "most productivity tools are procrastination with better lighting",
     "format": "one-liner"},
    {"title": "builder micro-story",
     "angle": "yesterday's bug that took 3 hours and taught a simple lesson",
     "format": "one-liner"},
    {"title": "workflow I actually use",
     "angle": "one boring habit that compounds better than any hack",
     "format": "one-liner"},
    {"title": "question for the niche",
     "angle": "ask the audience what they would have done differently",
     "format": "one-liner"},
]

TEMPERATURES = {"safe": 0.7, "bold": 0.95, "experimental": 1.15}


def eval_idea(i: int) -> dict:
    return EVAL_IDEAS[i % len(EVAL_IDEAS)]


def generate_post(ctx: EvalContext, idea: dict, temp: str = "safe",
                  language: Optional[str] = None) -> dict:
    """One draft through the product-shaped prompt path (no DB writes).

    Returns {"text", "language"} — voice-match re-roll is skipped: the harness
    measures raw generation quality, not the safety net behind it.
    """
    t = TEMPERATURES.get(temp, 0.7)
    user = f"""IDEA: {idea['title']}
ANGLE: {idea['angle']}
FORMAT: {idea.get('format', 'one-liner')}
Temperature intent: {temp} — {'play it straight, highest fidelity to voice' if temp == 'safe' else 'stronger opinion, bolder hook' if temp == 'bold' else 'unusual structure or framing, still on voice'}
{draft_language_instruction(language)}"""
    user += "\n\nWrite the post now."
    system = ctx.brain_prefix() + DRAFT_SYSTEM.format(
        voice=voice_prompt_block(language), algo=ALGO_PROMPT_BLOCK)
    raw = ctx.llm(ctx.cfg.llm, system=system, user=user,
                  temperature=t, json_mode=True)
    data = extract_json(raw)
    if isinstance(data, dict) and isinstance(data.get("thread"), list) and data["thread"]:
        text = str(data["thread"][0])
    else:
        text = str((data or {}).get("tweet", "")) if isinstance(data, dict) else ""
    return {"text": text[:500], "idea": idea["title"], "temp": temp}


def sample_posts(ctx: EvalContext, n: Optional[int] = None,
                 temps: tuple[str, ...] = ("safe",)) -> list[dict]:
    """n posts across the fixed eval ideas (round-robin temperatures)."""
    n = n or ctx.n
    out = []
    for i in range(n):
        out.append(generate_post(ctx, eval_idea(i), temp=temps[i % len(temps)]))
    return out
