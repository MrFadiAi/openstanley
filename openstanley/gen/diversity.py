"""Draft diversity — kill the sameness (user report 2026-08-24).

Three mechanisms:
1. similarity(): token-overlap vs recent drafts; the create loop retries
   once with a "be different" directive, then skips the idea.
2. FORMATS: a rotation of shapes so the model doesn't collapse onto
   colon-led one-liners; each create run gets a DIFFERENT format hint,
   and question-endings are capped (a question is ONE hook type, not
   the house style).
3. variety_block(): the prompt directive that carries both into the LLM.
"""
from __future__ import annotations

from typing import Optional

from ..core import db

SIM_TOO_HIGH = 0.55          # token-overlap vs any recent draft → too same
RECENT_WINDOW = 12           # drafts the similarity gate looks back on
MAX_QUESTION_SHARE = 0.34    # at most 1 in 3 recent drafts ends with '?'

FORMATS = [
    ("story", "open with a tiny concrete moment ('today i...', 'watched a guy...'), earn the takeaway"),
    ("observation", "a specific thing you noticed, stated plainly, no setup"),
    ("contrarian", "start from what everyone believes, then the turn ('everyone says X. actually...')"),
    ("utility", "share a real trick/capability with concrete detail, no listicle framing"),
    ("hot-take", "one strong claim, lowercase, no question mark"),
    ("dialogue", "reconstruct a real exchange or request in 2-3 lines"),
]


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "it",
            "i", "my", "you", "your", "for", "with", "that", "this"}
    return {w for w in (text or "").lower().replace("\n", " ").split()
            if w not in stop and len(w) > 2}


def similarity(text: str, other: str) -> float:
    a, b = _tokens(text), _tokens(other)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def recent_draft_texts(acct: Optional[int] = None, window: int = RECENT_WINDOW) -> list[str]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT text FROM drafts WHERE account_id=? "
            "AND status IN ('draft','approved') ORDER BY id DESC LIMIT ?",
            (db.active_account() if acct is None else acct, window)).fetchall()
    return [r["text"] for r in rows]


def too_similar(text: str, recent: list[str]) -> Optional[str]:
    """Returns the offending recent draft when similarity exceeds the bar.
    Texts with under two content tokens carry no angle to steal — skipped."""
    if len(_tokens(text)) < 2:
        return None
    for prev in recent:
        if similarity(text, prev) >= SIM_TOO_HIGH:
            return prev
    return None


def question_budget(recent: list[str]) -> bool:
    """True when another question-ending draft is allowed."""
    if not recent:
        return True
    share = sum(1 for t in recent if t.rstrip().endswith("?")) / len(recent)
    return share < MAX_QUESTION_SHARE


def format_for_run(run_index: int) -> tuple[str, str]:
    """Rotate through the shapes so consecutive drafts never match."""
    return FORMATS[run_index % len(FORMATS)]


def variety_block(recent: list[str], fmt: tuple[str, str],
                  allow_question: bool) -> str:
    """The prompt directive. Recent drafts are listed as forbidden angles."""
    lines = ["", "VARIETY (hard rules):",
             f"- Write a {fmt[0].upper()} post: {fmt[1]}."]
    if not allow_question:
        lines.append("- Do NOT end with a question. Make a statement instead.")
    else:
        lines.append("- A question ending is allowed but only if it earns the reply.")
    if recent:
        lines.append("- The drafts below already exist. Your post MUST bring a "
                     "DIFFERENT idea or a clearly different angle — do not "
                     "restate any of them:")
        for t in recent[:RECENT_WINDOW]:
            lines.append(f"  · {t[:110]}")
    lines.append("- Vary length run to run: some posts 40-90 chars, some "
                 "140-240. Not every post is a one-liner.")
    return "\n".join(lines)
