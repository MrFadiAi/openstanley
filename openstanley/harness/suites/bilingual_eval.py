"""Bilingual quality eval — AR / EN / mixed posts follow their conventions.

Requests posts in each language and verifies lang.py detection agrees with the
request, numerals stay in one system, and Arabic carries proper RTL
punctuation (؟ ، ؛) with no Persian lookalikes or tatweel.
"""
from __future__ import annotations

from ..base import EvalContext, generate_post
from ...gen.lang import arabic_issues, detect, numerals_style

CHECK_IDEAS = {
    "ar": {"title": "bilingual check: arabic",
           "angle": "write this post in natural Arabic following X Arabic conventions",
           "format": "one-liner"},
    "en": {"title": "bilingual check: english",
           "angle": "write this post in crisp English matching the account voice",
           "format": "one-liner"},
    "mixed": {"title": "bilingual check: mixed",
              "angle": "write this post mixing Arabic and English the way bilingual builders tweet",
              "format": "one-liner"},
}


def run(ctx: EvalContext) -> dict:
    rounds = max(1, (ctx.n + 2) // 3)
    cases: list[dict] = []
    for _ in range(rounds):
        for lang in ("ar", "en", "mixed"):
            p = generate_post(ctx, CHECK_IDEAS[lang], temp="safe", language=lang)
            detected = detect(p["text"])
            checks = {
                "language": detected == lang,
                "numerals": numerals_style(p["text"]) != "mixed",
                "rtl_punct": not arabic_issues(p["text"]),
            }
            passed = sum(checks.values())
            cases.append({"requested": lang, "detected": detected,
                          "text": p["text"][:140],
                          "checks": checks,
                          "issues": arabic_issues(p["text"]),
                          "passed": f"{passed}/3"})
    score = round(100 * sum(
        sum(c["checks"].values()) for c in cases) / (3 * max(1, len(cases))), 1)
    return {
        "score": score,
        "details": {"cases": cases,
                    "note": "mixed detection needs both scripts meaningfully present"},
    }
