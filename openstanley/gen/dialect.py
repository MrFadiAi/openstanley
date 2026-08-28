"""Dialect learning — the account's EXACT Arabic, mined from its own posts.

The owner's report (2026-08-28): "it has to be the perfect Arabic dialect,
dependent on the X account — study the account and know which sentences it
should use." GLM defaults to generic/Gulf-flavored Arabic; the account
writes Iraqi (evidence: اني ×29, شنو ×19, تعب ×11, ويه ×7 in 228 Arabic
posts). Guessing produced "not so logical" sentences in the wrong dialect.

This module makes the dialect a LEARNED, per-account fact:
- mine(): deterministic scan of own posts — dialect-family marker counts,
  the account's characteristic Arabic words, verbatim example sentences
- build_profile(): one LLM pass over real samples → key constructions
  (how THIS account says now / I / very / what / with), spelling habits,
  and a one-line voice directive; stored per account + a brain file
- dialect_block(): the prompt injection — family + markers + constructions
  + REAL sentences + the hard instruction. Wired into voice_prompt_block
  so every draft path (create, chat, TG, replies, trend, github) gets it.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Optional

from ..core import db
from ..core.config import Config

# Dialect families by characteristic markers. Substring counts (validated
# live on the account: unambiguous winners). Kept deliberately small —
# each marker must be STRONG evidence, not shared vocabulary.
MARKERS: dict[str, list[str]] = {
    "iraqi": ["هسه", "اكو", "ماكو", "اني ", "احنة", "حنة ", "ويه ",
              "چان", "برا ", "خوش", "هواية", "هيج", "شلونك", "زين",
              "تعب ", "معود", "گعد", "هو ", "انتة"],
    "gulf": ["صوب", "ويش", "مره ", "ابيك", "ودي ", "الحين", "شقا",
             "كذا", "ترى ", "يا بعد"],
    "levantine": ["شو ", "هيك", "كتير", "بدك", "هلق", "عنا ", "يلا "],
    "egyptian": ["ايه ", "كده", "اوريه", "عايز", "عشان", "بقي ", "خالص"],
    "msa": ["الذي", "التي ", "اللذين", "حيث ", "كذلك", "قمّة"],
}

_AR_CHAR = re.compile(r"[؀-ۿ]")
_WORD = re.compile(r"[؀-ۿ]{2,}")

# words too generic to mark dialect or style
_GENERIC_AR = {"هذا", "هذه", "ذلك", "اللي", "على", "من", "في", "عن", "مع",
               "هو", "هي", "انا", "انت", "احنا", "هم", "كان", "كانت",
               "لكن", "كل", "بعد", "قبل", "حتى", "اذا", "لما", "how",
               "what", "the", "and"}


def _is_arabic(text: str) -> bool:
    return bool(text) and len(_AR_CHAR.findall(text)) >= max(4, len(text) // 8)


def mine(acct: Optional[int] = None, max_posts: int = 400) -> dict:
    """Deterministic dialect scan of the account's own posts. Returns
    {family, confidence, evidence, char_words, examples} — examples are
    VERBATIM high-marker sentences for the prompt and the LLM pass."""
    posts = [p["text"] for p in db.own_posts(max_posts, acct=acct)
             if p.get("text") and _is_arabic(p["text"])]
    fam_counts: dict[str, Counter] = {f: Counter() for f in MARKERS}
    word_freq: Counter = Counter()
    for t in posts:
        for fam, words in MARKERS.items():
            for w in words:
                n = t.count(w)
                if n:
                    fam_counts[fam][w] += n
        for w in _WORD.findall(t):
            if w not in _GENERIC_AR:
                word_freq[w] += 1
    totals = {f: sum(c.values()) for f, c in fam_counts.items()}
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    family = ranked[0][0] if ranked and ranked[0][1] > 0 else None
    # examples: sentences densest in the winning family's markers
    examples: list[str] = []
    if family:
        def _density(t: str) -> int:
            return sum(t.count(w) for w in MARKERS[family])
        for t in sorted(posts, key=_density, reverse=True):
            for sent in re.split(r"[\n]+", t):
                sent = sent.strip()
                if 25 <= len(sent) <= 180 and _density(sent) >= 1 \
                        and sent not in examples:
                    examples.append(sent)
            if len(examples) >= 14:
                break
    runner_up = ranked[1] if len(ranked) > 1 else (None, 0)
    confidence = round(
        ranked[0][1] / max(1, ranked[0][1] + runner_up[1]), 2) if family else 0.0
    return {
        "family": family,
        "confidence": confidence,
        "markers": {f: dict(c.most_common(6)) for f, c in fam_counts.items()
                    if sum(c.values())},
        "char_words": [w for w, _ in word_freq.most_common(20)],
        "examples": examples[:14],
        "posts_scanned": len(posts),
    }


REPORT_SYSTEM = """You are a dialectologist analyzing ONE X account's Arabic.
Below are real sentences the account wrote. Identify the EXACT dialect and
how this specific account speaks. Return STRICT JSON:
{"family": "iraqi|gulf|levantine|egyptian|msa|mixed",
 "constructions": ["how this account ACTUALLY says things — e.g. '\"now\" = هسه',
                    '\"I\" = اني', '\"with\" = ويه' — 6-12 items, each grounded
                    in the samples"],
 "spelling_habits": ["how they spell/romanize/punctuate — 2-5 items"],
 "avoid": ["dialects/forms that would sound WRONG for this account — 2-5"],
 "directive": "ONE imperative line a drafting model must obey, <=200 chars"}
Ground EVERY item in the samples; never invent a construction not visible
there."""


def build_profile(cfg: Config, acct: Optional[int] = None) -> Optional[dict]:
    """mine() + one LLM report → stored per account (+ brain file)."""
    from .llm import chat as llm_chat, extract_json, LLMError
    m = mine(acct)
    if not m["family"] or not m["examples"]:
        return None
    samples = "\n".join(f"- {e}" for e in m["examples"][:12])
    try:
        raw = llm_chat(cfg.llm, system=REPORT_SYSTEM, user=samples,
                       temperature=0.2, json_mode=True, thinking_budget=1500)
        report = extract_json(raw)
        if not isinstance(report, dict) or not report.get("family"):
            raise LLMError("bad dialect report")
    except LLMError as e:  # deterministic half still works without the LLM
        db.log("dialect", f"dialect LLM report skipped: {e}", level="warn")
        report = {}
    profile = {
        "family": report.get("family") or m["family"],
        "confidence": m["confidence"],
        "markers": m["markers"].get(m["family"] or "", {}),
        "char_words": m["char_words"][:15],
        "constructions": report.get("constructions") or [],
        "spelling_habits": report.get("spelling_habits") or [],
        "avoid": report.get("avoid") or [],
        "directive": report.get("directive") or
        f"Write Arabic ONLY in this account's {m['family']} dialect, using "
        f"its real constructions.",
        "examples": m["examples"][:10],
        "posts_scanned": m["posts_scanned"],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    db.set_acct_setting("dialect_profile", profile, acct=acct)
    try:  # human-visible copy in the brain
        from . import brain as brain_mod
        lines = [f"# Dialect Profile ({profile['family']}, "
                 f"confidence {profile['confidence']})", "",
                 f"- mined from {profile['posts_scanned']} Arabic posts"]
        if profile["constructions"]:
            lines.append("\n## Constructions")
            lines += [f"- {c}" for c in profile["constructions"]]
        if profile["avoid"]:
            lines.append("\n## Avoid")
            lines += [f"- {a}" for a in profile["avoid"]]
        lines.append("\n## Real sentences (verbatim)")
        lines += [f"- {e}" for e in profile["examples"]]
        brain_mod.write("files/dialect", "\n".join(lines) + "\n", acct=acct)
    except Exception as e:  # noqa: BLE001 — the brain copy is cosmetic
        db.log("dialect", f"brain file write skipped: {e}", level="warn")
    db.log("dialect", f"profile built: {profile['family']} "
                     f"(confidence {profile['confidence']}, "
                     f"{profile['posts_scanned']} posts)")
    return profile


def dialect_block(acct: Optional[int] = None) -> str:
    """The prompt injection — wired into voice_prompt_block so EVERY draft
    path carries the account's exact dialect with real evidence."""
    p = db.get_acct_setting("dialect_profile", acct=acct)
    if not isinstance(p, dict) or not p.get("family"):
        return ""
    out = [f"DIALECT — this account writes {p['family'].upper()} Arabic. "
           f"This is a measured fact from its own posts, NOT a guess:"]
    if p.get("markers"):
        mk = ", ".join(f"{w} (×{n})" for w, n in
                       list(p["markers"].items())[:8])
        out.append(f"- signature words: {mk}")
    if p.get("char_words"):
        out.append(f"- its frequent words: "
                   f"{', '.join(p['char_words'][:12])}")
    for c in (p.get("constructions") or [])[:10]:
        out.append(f"- construction: {c}")
    for h in (p.get("spelling_habits") or [])[:4]:
        out.append(f"- spelling: {h}")
    if p.get("avoid"):
        out.append(f"- NEVER mix in: {', '.join(p['avoid'][:5])}")
    if p.get("examples"):
        out.append("\nREAL SENTENCES this account wrote — match this "
                   "grammar and word choice exactly:")
        out += [f"«{e}»" for e in p["examples"][:8]]
    out.append(f"\nDIALECT RULE: {p.get('directive') or ''} Every Arabic "
               f"sentence must be one this account could plausibly have "
               f"written. If unsure how to say something in this dialect, "
               f"rephrase rather than drift to MSA or another dialect.")
    return "\n".join(out)
