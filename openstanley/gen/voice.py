"""Voice learning: bilingual style rubric + few-shot example curation.

v0.3: builds a rubric PER LANGUAGE when the account posts in both Arabic and
English, merges the measured style_profile into every prompt, and exposes a
voice-match pass used to check drafts before they finalize.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from ..core import db
from .lang import detect
from .llm import chat, extract_json, LLMError
from .style_scan import style_prompt_block
from ..core.config import Config

RUBRIC_SYSTEM = """You are a writing-style analyst. You extract a precise VOICE FINGERPRINT from a person's posts.
Return STRICT JSON only, schema:
{
  "diction": "vocabulary level, slang, jargon, Arabic/English mix if any",
  "tone": "e.g. dry humor, earnest, provocative, teacher-like",
  "structure": "sentence length patterns, line breaks, lists, hooks style",
  "casing_punctuation": "lowercase? periods? em-dashes? ellipses?",
  "emoji_use": "none/rare/specific ones",
  "signature_moves": "recurring rhetorical devices (contrast, callbacks, open loops)",
  "avg_length_chars": 120,
  "do_not": "things the voice never does (no hashtags, no 'Dear community', etc)",
  "persona_summary": "one sentence: who this account sounds like"
}

If the posts are Arabic: describe the Arabic register (MSA vs dialect, which),
numeral system used (Arabic-Indic ٠١٢ vs Western 012), and punctuation habits
(؟ ، ؛). Return the JSON values in English but quote Arabic examples inline."""


def build_voice(cfg: Config, force: bool = False,
                acct: int | None = None) -> dict:
    """Extract/update bilingual voice profile for one account's posts."""
    existing = db.load_voice(acct)
    posts = _own_writing(db.own_posts(limit=300, acct=acct))
    if not posts:
        raise LLMError("No own posts imported yet — run an import first.")

    examples = _top_examples(posts, k=8)
    if existing and not force:
        rubric = existing["rubric"]
        # keep rubric, refresh examples only (cheap path)
        db.save_voice(rubric, examples, acct)
        db.log("learn", f"[account {db._acct(acct)}] voice examples refreshed "
                        f"({len(examples)} examples)")
        return db.load_voice(acct)

    # split corpus by language — learn the voice in each language present
    by_lang: dict[str, list[dict]] = {"ar": [], "en": [], "mixed": []}
    for p in posts:
        if p.get("text"):
            by_lang[detect(p["text"])].append(p)

    rubrics: dict[str, dict] = {}
    for lang, corpus_posts in by_lang.items():
        if len(corpus_posts) < 8:
            continue  # not enough signal for a separate rubric
        scored = _scored(corpus_posts)
        corpus = "\n\n".join(f"POST {i+1} (likes {p['likes']}): {p['text']}"
                             for i, (_, p) in enumerate(scored[:40]))
        raw = chat(cfg.llm, RUBRIC_SYSTEM,
                   f"These posts are {lang.upper()}.\nAnalyze:\n\n{corpus}",
                   temperature=0.3, json_mode=True)
        rubrics[lang] = extract_json(raw)

    if not rubrics:  # degenerate corpus — fall back to whatever exists
        raise LLMError("Not enough posts per language to extract a voice rubric.")

    primary = max(rubrics, key=lambda k: len(by_lang[k]))
    rubric_obj = {"languages": rubrics, "primary": primary}
    db.save_voice(json.dumps(rubric_obj, ensure_ascii=False, indent=1),
                  examples, acct)
    db.log("learn", f"[account {db._acct(acct)}] voice rubric extracted "
                    f"(langs={list(rubrics)}, {len(examples)} examples)")
    return db.load_voice(acct)


def _own_writing(posts: list[dict]) -> list[dict]:
    """The owner's OWN writing only — retweets (RT @...) and bare links are
    AMPLIFIED content, not their voice (live 2026-08-29: the profile's top
    examples were RT @GoogleDeepMind / RT @BybitArabic promo posts, so every
    draft came out like a corporate announcement instead of the owner)."""
    out = []
    for p in posts:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        if t.upper().startswith("RT @"):
            continue
        if t.startswith("@") and " " not in t.split(" ")[0]:
            continue  # bare mention-acknowledgements
        out.append(p)
    return out


def _scored(posts: list[dict]) -> list[tuple[float, dict]]:
    scored = []
    for p in posts:
        eng = p.get("likes", 0) + 2 * p.get("reposts", 0) + 3 * p.get("replies", 0)
        imp = p.get("impressions") or 1
        scored.append((eng / max(imp, 1) * math.log2(imp + 2), p))
    scored.sort(key=lambda x: -x[0])
    return scored


def _top_examples(posts: list[dict], k: int = 8) -> list[dict]:
    def score(p):
        imp = p.get("impressions") or 1
        eng = p.get("likes", 0) + 2 * p.get("reposts", 0) + 3 * p.get("replies", 0)
        return eng / max(imp, 1)

    ranked = sorted(posts, key=score, reverse=True)
    return [{"text": p["text"], "likes": p.get("likes", 0), "score": round(score(p), 5)}
            for p in ranked[:k] if p.get("text")]


def _parse_rubric(rubric_text: str) -> dict:
    """Rubric is stored as JSON text; tolerate old single-rubric format."""
    try:
        parsed = json.loads(rubric_text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {"_raw": rubric_text}


def rubric_for(lang: Optional[str] = None) -> str:
    """Best rubric text for a target language ('ar'/'en'/None)."""
    v = db.load_voice()
    if not v:
        return ""
    obj = _parse_rubric(v["rubric"])
    if "languages" in obj:
        langs = obj["languages"]
        if lang and lang in langs:
            return json.dumps(langs[lang], ensure_ascii=False)
        primary = obj.get("primary", next(iter(langs)))
        return json.dumps(langs[primary], ensure_ascii=False)
    return v["rubric"]  # legacy single-rubric string


def voice_prompt_block(lang: Optional[str] = None) -> str:
    """Inject into every generation prompt: rubric + examples + measured style."""
    v = db.load_voice()
    style = style_prompt_block()
    if not v:
        base = "Voice: unknown yet — write in a natural, concise creator voice."
    else:
        examples = "\n---\n".join(e["text"] for e in v.get("examples", [])[:6])
        base = f"""VOICE FINGERPRINT (match this exactly):
{rubric_for(lang)}

EXAMPLES OF THE VOICE:
{examples}"""
    if lang == "ar":
        base += "\n\nWhen writing Arabic, stay strictly within the Arabic rubric."
    if lang != "en":  # Arabic or unspecified/mixed drafts carry the
        # dialect; explicit English never does
        try:  # the account's EXACT dialect, mined from its posts
            from .dialect import dialect_block
            dblock = dialect_block()
            if dblock:
                base += "\n\n" + dblock
        except Exception:  # noqa: BLE001 - dialect never breaks drafting
            pass
    if style:
        base += "\n\n" + style
    return base
