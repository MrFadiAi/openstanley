"""Draft generation — OpenStanley's "posts scripted in your voice".

v0.3: drafts are born algorithm-fit (scoring factors feed the prompt), can be
requested in Arabic or English, can quote tweets, and every draft passes a
voice-match check against the measured style profile before finalizing.
"""
from __future__ import annotations

import random

from ..core import db
from . import brain as brain_mod
from . import voice_lock
from .algorithm import score_draft, PROMPT_BLOCK as ALGO_PROMPT_BLOCK
from .lang import detect, draft_language_instruction
from .llm import chat, extract_json, LLMError
from .style_scan import voice_match
from .voice import voice_prompt_block
from ..core.config import Config

DRAFT_SYSTEM = """You are a ghostwriter that writes X (Twitter) posts AS the account owner.
{voice}

GROWTH HEURISTICS (X algorithm 2025-26 — replies are worth 27-75x likes):
{algo}

Rules:
- Sound EXACTLY like the examples. Copy the casing, punctuation, rhythm, emoji habits.
- Hard limit 280 chars per tweet (unless thread).
- No hashtags unless the voice uses them. No "🧵" emoji unless voice does.
- If format is "thread": first tweet is the hook (no context, standalone scroll-stopper),
  then 3-7 tweets, each < 280 chars, numbered by the reader naturally.
- If QUOTED TWEET context is provided: your post is the comment ON TOP of the
  quote — add the angle the original lacks; never repeat its text; do not use
  quote marks around it.
- Return STRICT JSON: {{"tweet": "..."}} for single posts,
  {{"thread": ["hook", "2/ ...", "3/ ..."]}} for threads."""

VOICE_MATCH_THRESHOLD = 55


def generate_drafts(cfg: Config, count: int = None) -> list[int]:
    """Create `count` drafts from the freshest ideas. Returns draft ids."""
    count = count or cfg.agent.daily_draft_target
    ideas = db.fresh_ideas(limit=count)
    if not ideas:
        db.log("create", "no fresh ideas — run study loop first")
        return []

    temps = ["safe", "bold", "experimental"]
    draft_ids = []
    for i, idea in enumerate(ideas[:count]):
        temp = temps[i % len(temps)]
        # language rotation for bilingual accounts: follow the profile mix
        lang = _language_rotation()
        try:
            draft = _draft_one(cfg, idea, temp, language=lang)
        except LLMError as e:
            db.log("create", f"draft failed for idea {idea['id']}: {e}", level="error")
            continue
        if draft is None:  # voice-lock rejection — idea stays fresh for a retry
            continue
        did = db.add_draft(
            text=draft["text"], idea_id=idea["id"],
            kind=draft.get("kind", "post"),
            thread=draft.get("thread"), temperature=temp,
            image=draft.get("image"),
            quote_of=(draft.get("quote") or {}).get("x_id") if draft.get("quote") else None,
            meta=_draft_meta(idea, draft, lang),
        )
        db.mark_idea(idea["id"], "drafted")
        draft_ids.append(did)
    db.log("create", f"generated {len(draft_ids)} drafts")
    return draft_ids


def _draft_meta(idea: dict, draft: dict, lang: str) -> dict:
    meta = {"idea_title": idea.get("title", ""), "idea_angle": idea.get("angle", ""),
            "format": idea.get("format", "one-liner"), "source": idea.get("source", ""),
            "language": detect(draft["text"])}
    if draft.get("quote"):
        meta["quote"] = draft["quote"]
    if draft.get("voice_lock"):
        meta["voice"] = draft["voice_lock"]
    alg = draft.get("alg") or score_draft_row_lite(draft)
    meta["alg"] = alg
    meta["voice_match"] = voice_match(draft["text"])
    return meta


def score_draft_row_lite(draft: dict) -> dict:
    from .algorithm import score_draft_row
    row = {"text": draft["text"], "kind": draft.get("kind", "post"),
           "image": draft.get("image"), "thread": draft.get("thread"),
           "meta": {}, "scheduled_at": None}
    return score_draft_row(row)


def _language_rotation() -> str | None:
    """Pick draft language by the account's measured mix (None = let voice decide)."""
    profile = db.get_setting("style_profile") or {}
    mix = (profile.get("stats") or {}).get("language_mix") or {}
    ar, en = mix.get("ar", 0), mix.get("en", 0)
    if ar > 0.25 and en > 0.25:  # genuinely bilingual → rotate proportionally
        return "ar" if random.random() < ar / (ar + en) else "en"
    if ar > 0.6:
        return "ar"
    if en > 0.6:
        return "en"
    return None


def regenerate(draft_id: int) -> int:
    """Regenerate a single draft (new variation, hotter temperature). Returns new id."""
    src = db.get_draft(draft_id)
    if not src:
        raise LLMError(f"draft {draft_id} not found")
    cfg = _cfg()
    idea = {"id": src["idea_id"], "title": src["meta"].get("idea_title", ""),
            "angle": src["meta"].get("idea_angle", ""), "format": src["meta"].get("format", "one-liner"),
            "source": ""}
    temp = src["temperature"] if src["temperature"] != "safe" else "bold"
    lang = src["meta"].get("language")
    draft = _draft_one(cfg, idea, temp, language=lang,
                       quote=src["meta"].get("quote"), image=src.get("image"))
    if draft is None:
        raise LLMError("voice lock rejected the regenerated draft")
    did = db.add_draft(text=draft["text"], idea_id=src["idea_id"],
                       kind=src.get("kind", "post"), thread=draft.get("thread"),
                       temperature=temp, image=draft.get("image"),
                       quote_of=src.get("quote_of"), meta=_draft_meta(idea, draft, lang or "en"))
    return did


def generate_quote_draft(cfg: Config, tweet: dict, angle: str = "") -> int:
    """Draft a quote post commenting on `tweet` (an important niche announcement)."""
    idea = {"id": None, "title": "Quote post", "angle": angle or "add the angle the original lacks",
            "format": "quote", "source": f"quote of @{tweet.get('author', '')}"}
    draft = _draft_one(cfg, idea, "bold", language=None,
                       quote={"x_id": tweet["x_id"], "url": tweet.get("url", ""),
                              "text": tweet.get("text", ""), "author": tweet.get("author", "")})
    if draft is None:
        raise LLMError("voice lock rejected the quote draft")
    meta = _draft_meta(idea, draft, detect(draft["text"]))
    meta["quote"] = draft.get("quote")
    return db.add_draft(text=draft["text"], kind="quote", temperature="bold",
                        quote_of=tweet["x_id"], meta=meta)


def _draft_one(cfg: Config, idea: dict, temp: str, language: str | None = None,
               quote: dict | None = None, image: str | None = None) -> dict | None:
    t = {"safe": 0.7, "bold": 0.95, "experimental": 1.15}[temp]
    voice = voice_prompt_block(language)
    user = f"""IDEA: {idea['title']}
ANGLE: {idea['angle']}
FORMAT: {idea.get('format', 'one-liner')}
Temperature intent: {temp} — {'play it straight, highest fidelity to voice' if temp == 'safe' else 'stronger opinion, bolder hook' if temp == 'bold' else 'unusual structure or framing, still on voice'}
{draft_language_instruction(language)}"""
    if quote:
        user += f"""
QUOTED TWEET (your post is the comment above it):
@{quote.get('author', '?')}: {quote.get('text', '')[:240]}"""
    user += "\n\nWrite the post now."
    system = brain_mod.brain_context() + "\n\n" + \
        DRAFT_SYSTEM.format(voice=voice, algo=ALGO_PROMPT_BLOCK)
    raw = chat(cfg.llm, system, user, temperature=t, json_mode=True)
    data = extract_json(raw)
    out: dict = {"image": image}
    if quote:
        out["quote"] = quote
        out["kind"] = "quote"
    if "thread" in data and isinstance(data["thread"], list) and data["thread"]:
        out["text"] = data["thread"][0]
        out["thread"] = [str(x) for x in data["thread"]]
    else:
        out["text"] = str(data.get("tweet", ""))[:500]
        out["kind"] = out.get("kind", "post")

    # voice-match pass: check the draft against the measured style profile;
    # one re-roll with explicit feedback if it lands far off
    out["alg"] = score_draft_row_lite(out)
    vm = voice_match(out["text"])
    out["voice_match"] = vm
    if vm < VOICE_MATCH_THRESHOLD:
        try:
            raw2 = chat(cfg.llm, system,
                        user + f"\n\nYour previous attempt scored only {vm}% voice "
                               f"match. Fix the style drift and rewrite.",
                        temperature=t, json_mode=True)
            data2 = extract_json(raw2)
            if "thread" in data2 and isinstance(data2["thread"], list) and data2["thread"]:
                cand = str(data2["thread"][0])
            else:
                cand = str(data2.get("tweet", ""))
            if cand and voice_match(cand) >= vm:
                out["text"] = cand[:500]
                out["voice_match"] = voice_match(cand)
                out["alg"] = score_draft_row_lite(out)
        except LLMError:
            pass  # keep first attempt

    # voice lock (v0.4.0): score / fix / reject BEFORE the draft is stored.
    # Rejected → None; the caller skips storing and the idea stays fresh.
    locked, vmeta = voice_lock.apply_voice_lock(cfg, out["text"],
                                                out.get("kind", "post"))
    if locked is None:
        return None
    if locked != out["text"]:  # the lock rewrote it → re-score the stored text
        out["voice_match"] = voice_match(locked)
        out["alg"] = score_draft_row_lite({**out, "text": locked})
    out["text"] = locked
    out["voice_lock"] = vmeta
    return out


def _cfg() -> Config:
    from ..core.config import load_config
    return load_config()
