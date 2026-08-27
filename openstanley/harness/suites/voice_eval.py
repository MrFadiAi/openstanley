"""Voice fidelity eval — do generated drafts actually sound like the account?

N sample drafts generated from the voice profile; each scored for voice-match
via style_profile distance: vocab overlap, sentence-length distribution,
punctuation habits, emoji density → 0-100 per draft, averaged for the suite.
"""
from __future__ import annotations

import re
from typing import Optional

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
    mean_combined = round(
        sum(s["combined"] for s in samples) / max(1, len(samples)), 1)
    base = real_post_baseline(profile)
    if base:
        # CALIBRATED score: drafts measured RELATIVE to the account's own
        # posts on the same metrics. The absolute scale is compressed (live
        # account 2: real posts 61.7 vs drafts 62.2 — drafts already match
        # reality), so "100" now means "indistinguishable from — or better
        # than — your own writing", which is the actual product goal.
        rel = round(100 * mean_combined / max(base["combined"], 1.0), 1)
        score = min(rel, 100.0)
        note = (f"calibrated vs {base['posts']} real posts "
                f"(baseline {base['combined']})")
        if rel > 100.0:
            note += f" — drafts EXCEED the real-post baseline by {rel - 100:.1f}"
    else:
        score = mean_combined
        note = ""
    return {
        "score": score,
        "details": {
            "samples": samples,
            "mean_voice_match": round(
                sum(s["voice_match"] for s in samples) / max(1, len(samples)), 1),
            "mean_style_distance": round(
                sum(s["style_distance"] for s in samples) / max(1, len(samples)), 1),
            "mean_combined": mean_combined,
            "real_post_baseline": base,
            "profile_present": bool(profile),
            "note": note or ("no style profile yet — run a scan for a sharper "
                            "baseline" if not profile
                            else "no own posts yet — absolute scale, will "
                                 "calibrate once the account has history"),
        },
    }


def real_post_baseline(profile: dict | None, limit: int = 25) -> Optional[dict]:
    """Score the account's OWN recent posts on the same metrics — the
    ground-truth anchor. Returns None with under 5 posts (a fresh account
    keeps the absolute scale rather than anchoring on noise)."""
    from ...core import db
    posts = [p for p in db.own_posts(limit) if (p.get("text") or "").strip()]
    if len(posts) < 5:
        return None
    vms = [voice_match(p["text"], profile) for p in posts]
    sds = [style_distance(p["text"], profile) for p in posts]
    comb = [round(0.6 * v + 0.4 * s, 1) for v, s in zip(vms, sds)]
    return {
        "posts": len(posts),
        "voice_match": round(sum(vms) / len(vms), 1),
        "style_distance": round(sum(sds) / len(sds), 1),
        "combined": round(sum(comb) / len(comb), 1),
    }
