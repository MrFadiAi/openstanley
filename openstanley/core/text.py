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
    # OWNER DIRECTIVE (2026-08-29): NEVER any dash at all. List-bullet
    # hyphens and hyphenated compounds survived the em/en scrub — line
    # leading dashes are dropped (the owner's lists run bare), inline
    # hyphens become spaces. URLs are exempt (their dashes are structural).
    import re as _re
    urls: list[str] = []
    def _stash(m):
        urls.append(m.group(0))
        return f"\x00{len(urls) - 1}\x00"
    out = _re.sub(r"https?://\S+|www\.\S+", _stash, out)
    out = "\n".join(
        (ln.lstrip("- ").lstrip() if ln.lstrip().startswith("-") else ln)
        for ln in out.split("\n"))
    out = out.replace("-", " ")
    while "  " in out:
        out = out.replace("  ", " ")
    if urls:
        out = _re.sub("\x00(\\d+)\x00",
                      lambda m: urls[int(m.group(1))], out)
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
