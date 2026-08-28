"""Bilingual (Arabic/English) language utilities — detection, conventions, RTL.

The account may post in Arabic, English, or mixed. Everything downstream
(voice, drafts, chat, algorithm scoring) asks this module what language a text
is in and whether the Arabic follows X Arabic conventions.
"""
from __future__ import annotations

import re
from typing import Literal

Lang = Literal["ar", "en", "mixed"]

_AR_CHARS = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")
_LATIN_CHARS = re.compile(r"[A-Za-z]")
_AR_DIGITS = re.compile(r"[٠-٩]")
_EN_DIGITS = re.compile(r"[0-9]")
# Persian/Urdu lookalikes that break Arabic text rendering on X
_PERSIAN_LOOKALIKES = {"ی": "ي", "ک": "ك", "ھ": "ه", "ې": "ي", "گ": "ج"}
_TATWEEL = "ـ"


def arabic_ratio(text: str) -> float:
    """Share of Arabic letters among all letters."""
    ar = len(_AR_CHARS.findall(text))
    lat = len(_LATIN_CHARS.findall(text))
    if ar + lat == 0:
        return 0.0
    return ar / (ar + lat)


def detect(text: str) -> Lang:
    """'ar' | 'en' | 'mixed' by letter-script share."""
    ratio = arabic_ratio(text)
    if ratio >= 0.75:
        return "ar"
    if ratio <= 0.20:
        return "en"
    return "mixed"


def numerals_style(text: str) -> Literal["western", "arabic-indic", "mixed", "none"]:
    has_ar = bool(_AR_DIGITS.search(text))
    has_en = bool(_EN_DIGITS.search(text))
    if has_ar and has_en:
        return "mixed"
    if has_ar:
        return "arabic-indic"
    if has_en:
        return "western"
    return "none"


def arabic_issues(text: str) -> list[str]:
    """Convention violations that make Arabic posts look broken on X.

    Rules (X Arabic conventions):
    - question mark ؟ / comma ، / semicolon ؛ instead of Western ones
    - no Persian lookalike letters (ی ك ھ) inside Arabic text
    - one numeral system per post (Arabic-Indic ٠١٢ or Western 012 — not both)
    - no tatweel stretch (ــــ)
    """
    issues: list[str] = []
    if arabic_ratio(text) < 0.30:
        return issues  # not Arabic enough to judge
    # Western punctuation in an Arabic sentence
    if _AR_CHARS.search(text):
        # a '?' adjacent to Arabic chars should be ؟
        if re.search(r"[؀-ۿ]\s*\?|\?\s*[؀-ۿ]", text):
            issues.append("Western '?' next to Arabic — use '؟'")
        if re.search(r"[؀-ۿ]\s*,|,\s*[؀-ۿ]", text):
            issues.append("Western ',' next to Arabic — use '،'")
        if re.search(r"[؀-ۿ]\s*;|;\s*[؀-ۿ]", text):
            issues.append("Western ';' next to Arabic — use '؛'")
    for bad in _PERSIAN_LOOKALIKES:
        if bad in text:
            issues.append(f"Persian letter '{bad}' — use Arabic "
                          f"'{_PERSIAN_LOOKALIKES[bad]}'")
    if numerals_style(text) == "mixed":
        issues.append("mixed Western + Arabic-Indic numerals — pick one system")
    # RUNS only (2+ consecutive): a SINGLE tatweel is the standard الـ
    # definite article before Latin loanwords ("بالـ API") — the owner's own
    # posts use it 7/50 with zero runs (2026-08-28); penalizing it punished
    # the account's real voice on 8 of 12 eval drafts.
    if _TATWEEL * 2 in text:
        issues.append("tatweel stretch (ـ) renders broken on X")
    return issues


AR_GENERATION_RULES = """ARABIC POST CONVENTIONS (when writing in Arabic):
- Use Arabic punctuation: ؟ للسؤال ، للفاصلة ؛ للفاصلة المنقوطة — never
  Western ? , ; inside Arabic text.
- Numerals: match the account's system (Arabic-Indic ٠١٢٣ or Western 0123,
  never mix both in one post).
- Pure MSA or Gulf colloquial matching the account — no machine-translation
  stiffness (no "عزيزي المستخدم", no news-anchor register).
- Handle حاء-family letters correctly: write proper hamza forms (أ إ آ ؤ ئ ء),
  never drop or swap them.
- No Persian letters (ی ك ھ) — always Arabic ي ك ه.
- RTL-safe: Latin words (brand names, code) are fine but keep them isolated
  tokens with spaces around them."""


def reply_language_instruction(user_text: str) -> str:
    """Instruction for the chat LLM: answer in the user's language."""
    lang = detect(user_text)
    if lang == "ar":
        return "Reply in Arabic (natural, modern — match the user's register)."
    if lang == "mixed":
        return ("Reply in the same Arabic/English mix the user used — mirror "
                "their dominant language.")
    return "Reply in English."


def draft_language_instruction(lang: str | None) -> str:
    """Instruction for the draft LLM when a specific language is requested."""
    if lang == "ar":
        return "\nWrite the post in ARABIC.\n" + AR_GENERATION_RULES
    if lang == "en":
        return "\nWrite the post in ENGLISH."
    return ""  # let the voice decide (mixed accounts)
