"""Deep account scan — the rich `style_profile` behind voice matching.

Pulls up to 800 recent posts + replies (batched, rate-limit safe), computes a
structured style fingerprint locally (no LLM needed for the numbers), then asks
the LLM for one human-readable summary. Stored as settings key `style_profile`
and consumed by voice learning, draft generation, and the voice-match pass.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from typing import Optional

from ..core import db
from .lang import arabic_ratio, detect
from .llm import chat, extract_json, LLMError
from .voice_lock import count_misspellings, write_voice_md

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")
_HUMOR_MARKERS = ["😂", "😅", "lol", "lmao", "haha", "ههه", "😂😂", "jk",
                  "kidding", "ironic", "sarcasm"]
_STOP = set("""a an the and or but if then of to in on for with at by from is are
was were be been i you he she it we they my your our this that these those as
it's i'm don't do does did not no so just very really ما هذا من في على عن إلى
التي الذي""".split())


async def scan_account(cfg, x_client, max_posts: int = 800) -> dict:
    """Deep-scan the connected account (async — shares the agent's event loop).

    The final LLM summary is a blocking httpx call; run this whole coroutine
    via `asyncio.to_thread`-style thread + `asyncio.run`, or call it from the
    server where its sync tail is acceptable.
    """
    me = db.get_setting("me") or await x_client.me()
    username = me.get("username") or cfg.x.username
    db.log("scan", f"deep scan starting for @{username} (cap {max_posts})")

    posts: list[dict] = []
    seen: set[str] = set()
    # batched pulls — 100/page, stop early when the timeline runs dry
    for offset_start in range(0, max_posts, 100):
        want = min(100, max_posts - offset_start)
        try:
            batch = await x_client.user_tweets(username, limit=want)
        except Exception as e:  # noqa: BLE001
            db.log("scan", f"user_tweets batch failed: {e}", level="warn")
            break
        new = [p for p in batch if p.get("x_id") and p["x_id"] not in seen]
        if not new:
            break
        for p in new:
            seen.add(p["x_id"])
            posts.append(p)
        if len(batch) < want:
            break
    # replies deepen the style fingerprint (short-form register)
    try:
        reply_batch = await x_client.user_replies(username, limit=min(200, max_posts))
    except Exception as e:  # noqa: BLE001
        reply_batch = []
        db.log("scan", f"replies pull skipped: {e}", level="warn")
    for p in reply_batch:
        if p.get("x_id") and p["x_id"] not in seen:
            seen.add(p["x_id"])
            posts.append(p)

    for p in posts:
        db.upsert_post(p)

    stats = compute_stats(posts)
    summary = ""
    try:
        # blocking LLM call → worker thread so the event loop stays free
        summary = await asyncio.to_thread(_llm_summary, cfg, stats, posts)
    except LLMError as e:
        db.log("scan", f"LLM summary failed (stats kept): {e}", level="warn")

    profile = {
        "stats": stats,
        "human_summary": summary,
        "username": username,
        "updated_at": db._now(),
    }
    db.set_setting("style_profile", profile)
    # scan-derived persona keys for the voice lock (v0.4.0)
    if write_voice_md(stats):
        db.log("scan", "voice.md rewritten from scan stats — voice lock rules refreshed")
    db.log("scan", f"deep scan done: {len(posts)} posts scanned, "
                   f"languages={stats['language_mix']}")
    return profile


def compute_stats(posts: list[dict]) -> dict:
    """All-local style fingerprint — deterministic, no LLM."""
    texts = [p.get("text") or "" for p in posts if p.get("text")]
    n = max(1, len(texts))
    lens = [len(t) for t in texts]
    sents_all: list[int] = []
    punct = Counter()
    emoji = Counter()
    casing = Counter()
    hashtags = 0
    multiline = 0
    line_breaks = 0
    lang = Counter()
    humor = 0
    term_counts: Counter = Counter()
    hours = [0] * 24
    misspell = 0
    latin_words = 0

    for t in texts:
        for s in re.split(r"[.!?\n؟،]+", t):
            s = s.strip()
            if s:
                sents_all.append(len(s.split()))
        punct["excl"] += t.count("!")
        punct["question"] += t.count("?") + t.count("؟")
        punct["ellipsis"] += t.count("...")
        punct["emdash"] += t.count("—") + t.count("–")
        punct["colon"] += t.count(":")
        emoji.update(_EMOJI.findall(t))
        words = re.findall(r"[A-Za-z']+|[؀-ۿ]+", t)
        if t and t[0].islower():
            casing["lower_start"] += 1
        casing["allcaps"] += sum(1 for w in words if len(w) > 3 and w.isupper())
        hashtags += len(re.findall(r"#\w+", t))
        br = t.count("\n")
        line_breaks += br
        if br:
            multiline += 1
        lang[detect(t)] += 1
        if any(m in t.lower() for m in _HUMOR_MARKERS):
            humor += 1
        latin = re.findall(r"[A-Za-z']+", t)
        latin_words += len(latin)
        misspell += count_misspellings(t)
        for w in re.findall(r"[\w؀-ۿ]+", t.lower()):
            if len(w) > 3 and w not in _STOP:
                term_counts[w] += 1
    for p in posts:
        ca = p.get("created_at") or ""
        if ca and "T" in ca:
            try:
                hours[int(ca[11:13])] += 1
            except ValueError:
                pass

    total_words = sum(term_counts.values())
    sorted_terms = [w for w, _ in term_counts.most_common(40)]
    top_hours = sorted(range(24), key=lambda h: hours[h], reverse=True)[:3]
    hour_hist = hours
    best_hours = [h for h in top_hours if hours[h] >= max(1, len(posts) // 40)]

    sents_sorted = sorted(sents_all) or [0]
    return {
        "posts_scanned": len(texts),
        "avg_length_chars": round(sum(lens) / n, 1),
        "length_dist": {
            "under_80": sum(1 for l in lens if l < 80) / n,
            "80_160": sum(1 for l in lens if 80 <= l < 160) / n,
            "160_240": sum(1 for l in lens if 160 <= l < 240) / n,
            "over_240": sum(1 for l in lens if l >= 240) / n,
        },
        "sentence": {
            "avg": round(sum(sents_all) / max(1, len(sents_all)), 1),
            "p50": sents_sorted[len(sents_sorted) // 2],
            "p90": sents_sorted[int(len(sents_sorted) * 0.9) - 1 if len(sents_sorted) > 1 else 0],
        },
        "punctuation": {k: round(v / n, 3) for k, v in punct.items()},
        "emoji": {
            "per_post": round(sum(emoji.values()) / n, 2),
            "top": [e for e, _ in emoji.most_common(5)],
        },
        "hashtags": {
            "per_post": round(hashtags / n, 2),
            "pct_with": round(sum(1 for t in texts if "#" in t) / n, 3),
        },
        "casing": {
            "pct_lowercase_start": round(casing["lower_start"] / n, 3),
            "allcaps_words_per_post": round(casing["allcaps"] / n, 3),
        },
        "formatting": {
            "pct_multiline": round(multiline / n, 3),
            "avg_line_breaks": round(line_breaks / n, 2),
        },
        "vocabulary": {
            "top_terms": sorted_terms[:25],
            "uniqueness": round((len(term_counts) / total_words) if total_words else 0, 3),
        },
        "topics": _topics(term_counts),
        "posting_times": {
            "histogram": hour_hist,
            "best_hours": best_hours or [9, 13, 18],
        },
        "language_mix": {
            "ar": round(lang["ar"] / n, 3),
            "en": round(lang["en"] / n, 3),
            "mixed": round(lang["mixed"] / n, 3),
        },
        "humor_markers_per_post": round(humor / n, 3),
        "misspellings_per_100_words": round(misspell / latin_words * 100, 2)
        if latin_words else 0.0,
    }


def _topics(term_counts: Counter) -> list[str]:
    """Crude topic phrases: top bigrams by frequency (lightweight simclusters vibe)."""
    return [w for w, _ in term_counts.most_common(12)]


SUMMARY_SYSTEM = """You are a sharp writing-style analyst. Given computed style
statistics of an X (Twitter) account, write a compact human-readable summary
(5-8 sentences) of how this account writes: register, rhythm, punctuation
personality, emoji/hashtag habits, language mix, humor, best posting hours,
and 3 concrete "match this when writing as them" rules. If Arabic share is
significant, describe the Arabic register too. Return STRICT JSON:
{"summary": "..." , "humor_frequency": "none|rare|frequent|constant",
"sarcasm_note": "one sentence" }"""


def _llm_summary(cfg, stats: dict, posts: list[dict]) -> str:
    sample = "\n".join(f"- {p.get('text', '')[:120]}"
                       for p in posts[:25] if p.get("text"))
    user = (f"STYLE STATS:\n{json.dumps(stats, ensure_ascii=False)}\n\n"
            f"SAMPLE POSTS:\n{sample}\n\nWrite the summary.")
    data = extract_json(chat(cfg.llm, SUMMARY_SYSTEM, user,
                             temperature=0.3, json_mode=True))
    s = str(data.get("summary", ""))
    if data.get("humor_frequency"):
        s += f"\nHumor: {data['humor_frequency']}."
    if data.get("sarcasm_note"):
        s += f" {data['sarcasm_note']}"
    return s


def load_profile() -> Optional[dict]:
    return db.get_setting("style_profile")


def style_prompt_block() -> str:
    """Injected into generation prompts after the voice rubric."""
    p = load_profile()
    if not p:
        return ""
    s = p.get("stats") or {}
    return f"""STYLE PROFILE (measured from the account — match these numbers):
- avg post length {s.get('avg_length_chars', '?')} chars; sentence p50 {s.get('sentence', {}).get('p50', '?')} words
- emoji per post {s.get('emoji', {}).get('per_post', 0)} (top: {', '.join(s.get('emoji', {}).get('top', []) or [])})
- hashtags per post {s.get('hashtags', {}).get('per_post', 0)} ({int(s.get('hashtags', {}).get('pct_with', 0) * 100)}% of posts have any)
- starts lowercase {int(s.get('casing', {}).get('pct_lowercase_start', 0) * 100)}% of posts
- multiline {int(s.get('formatting', {}).get('pct_multiline', 0) * 100)}% of posts
- language mix: {s.get('language_mix')}
- humor markers per post: {s.get('humor_markers_per_post')}
{p.get('human_summary', '')}"""


def voice_match(text: str, profile: Optional[dict] = None) -> int:
    """0-100 heuristic match of a draft against the measured style profile."""
    profile = profile or load_profile()
    if not profile:
        return 60  # no profile yet → neutral score
    s = profile.get("stats") or {}
    score = 60.0
    notes: list[str] = []

    avg_len = float(s.get("avg_length_chars") or 140)
    delta = abs(len(text) - avg_len) / max(avg_len, 40)
    if delta > 0.8:
        score -= 15
        notes.append(f"length {len(text)} vs account avg {avg_len:.0f}")
    elif delta < 0.35:
        score += 10

    emoji_budget = float((s.get("emoji") or {}).get("per_post") or 0)
    draft_emoji = len(_EMOJI.findall(text))
    if draft_emoji > math.ceil(emoji_budget) + 2:
        score -= 10
        notes.append("more emoji than the account uses")
    if emoji_budget > 0.5 and draft_emoji == 0:
        score -= 5
        notes.append("account uses emoji; draft has none")

    if float((s.get("hashtags") or {}).get("pct_with") or 0) < 0.15 and "#" in text:
        score -= 12
        notes.append("hashtags don't fit this account")

    if float((s.get("casing") or {}).get("pct_lowercase_start") or 0) > 0.7:
        if text[:1].isupper():
            score -= 8
            notes.append("account writes lowercase; draft starts uppercase")

    mix = s.get("language_mix") or {}
    lang = detect(text)
    if mix.get(lang, 0) < 0.05 and sum(mix.values()) > 0.3:
        score -= 15
        notes.append(f"language '{lang}' rare in this account")

    return int(max(5, min(99, score)))
