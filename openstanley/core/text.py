"""Text hygiene shared by every layer (no gen imports — import-safe from core)."""
from __future__ import annotations


def scrub_ai_punctuation(text: str) -> str:
    """Em/en dashes read as machine-written (user rule 2026-08-20): no
    generated post, reply, thread or DM may carry one. Safe transform, not
    a rejection, a comma preserves the voice."""
    if not text:
        return text
    out = text
    for dash in ("\u2014", "\u2013", "\u2212"):
        out = out.replace(dash, ", ")
    out = out.replace(" -- ", ", ").replace(" --", ",")
    while "  " in out:
        out = out.replace("  ", " ")
    out = out.replace(" ,", ",").replace(", ", ", ")
    while ", ," in out or ",," in out:
        out = out.replace(", ,", ",").replace(",,", ",")
    return out


def err_str(e: BaseException) -> str:
    """str(e), falling back to repr when the exception carries no message.

    twikit/asyncio exceptions routinely str() to '' — live logs showed
    "search failed for 'X': " with the cause erased. This keeps the type
    visible so the next failure is diagnosable."""
    return str(e) or repr(e)
