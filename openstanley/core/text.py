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
    while ", ," in out:
        out = out.replace(", ,", ",")
    out = out.replace(" ,", ",")
    while "  " in out:
        out = out.replace("  ", " ")
    return out
