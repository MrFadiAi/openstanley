"""Voice fidelity eval — do generated drafts actually sound like the account?

N sample drafts generated from the voice profile; each scored for voice-match
via style_profile distance: vocab overlap, sentence-length distribution,
punctuation habits, emoji density → 0-100 per draft, averaged for the suite.
"""
from __future__ import annotations

import re

from ..base import EvalContext, sample_posts
from ...gen.style_scan import load_profile, voice_match

_STOP = set("""a an the and or but if then of to in on for with at by from is are
was were be been i you he she it we they my your our this that these those as
it's i'm don't do does did not no so just very really what's ما هذا من في على
عن إلى التي الذي""".split())
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w؀-ۿ']+", text.lower())
            if len(w) > 3 and w not in _STOP]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.!?\n؟،]+", text) if s.strip()]


def style_distance(text: str, profile: dict | None) -> float:
    """0-100 closeness of the draft to the measured style fingerprint."""
    if not profile:
        return 60.0  # no scan yet → neutral, voice_match carries the suite
    s = profile.get("stats") or {}
    parts: list[float] = []

    # 1. vocab overlap with the account's top terms
    terms = {t.lower() for t in (s.get("vocabulary") or {}).get("top_terms", [])}
    words = _content_words(text)
    if terms and words:
        parts.append(sum(1 for w in words if w in terms) / len(words))
    else:
        parts.append(0.5)

    # 2. sentence-length distribution vs profile p50
    p50 = float((s.get("sentence") or {}).get("p50") or 12)
    sents = _sentences(text)
    if sents:
        avg = sum(len(s.split()) for s in sents) / len(sents)
        parts.append(max(0.0, 1.0 - abs(avg - p50) / max(p50 * 2, 10)))
    else:
        parts.append(0.0)

    # 3. punctuation habits (question/exclaim/emdash per sentence)
    punct = s.get("punctuation") or {}
    if sents:
        q_rate = sum("?" in s or "؟" in s for s in sents) / len(sents)
        pq = min(1.0, float(punct.get("question") or 0))
        parts.append(max(0.0, 1.0 - abs(q_rate - pq)))
    else:
        parts.append(0.0)

    # 4. emoji density vs account budget
    budget = float((s.get("emoji") or {}).get("per_post") or 0)
    used = len(_EMOJI.findall(text))
    parts.append(max(0.0, 1.0 - abs(used - budget) / max(budget * 2, 3)))

    return round(100 * sum(parts) / len(parts), 1)


def run(ctx: EvalContext) -> dict:
    profile = load_profile()
    posts = sample_posts(ctx)
    samples = []
    for p in posts:
        vm = voice_match(p["text"], profile)
        sd = style_distance(p["text"], profile)
        combined = round(0.6 * vm + 0.4 * sd, 1)
        samples.append({"idea": p["idea"], "text": p["text"][:140],
                        "voice_match": vm, "style_distance": sd,
                        "combined": combined})
    score = round(sum(s["combined"] for s in samples) / max(1, len(samples)), 1)
    return {
        "score": score,
        "details": {
            "samples": samples,
            "mean_voice_match": round(
                sum(s["voice_match"] for s in samples) / max(1, len(samples)), 1),
            "mean_style_distance": round(
                sum(s["style_distance"] for s in samples) / max(1, len(samples)), 1),
            "profile_present": bool(profile),
            "note": "no style profile yet — run a scan for a sharper baseline"
                    if not profile else "",
        },
    }
