"""Tier-2 agent capabilities — repurpose, variants, digest, mention
replies, competitor study, learn-on-command (2026-08-30)."""
from __future__ import annotations

from ..core import db
from ..core.config import Config
from .tools import register


def remix_draft(cfg: Config, draft_id: int = 0, mode: str = "thread") -> dict:
    """Repurpose an existing draft: thread|shorter|longer|english|arabic|
    question|story. The new version lands as a fresh draft."""
    from .llm import chat as llm_chat, extract_json, LLMError
    from . import voice as voice_mod
    from ..core.text import scrub_ai_punctuation as scrub
    d = db.get_draft(draft_id)
    if not d:
        return {"ok": False, "error": f"no draft #{draft_id}"}
    modes = ("thread", "shorter", "longer", "english", "arabic",
             "question", "story")
    if mode not in modes:
        return {"ok": False, "error": f"mode must be one of {modes}"}
    lang = {"english": "en", "arabic": "ar"}.get(mode)
    directives = {
        "thread": "Split into a 3-5 tweet THREAD, each under 250 chars, "
                  "hook first.",
        "shorter": "Make it punchy: under 100 chars, keep the core idea.",
        "longer": "Expand with one concrete detail, still one post.",
        "english": "Rewrite entirely in English, same voice energy.",
        "arabic": "Rewrite entirely in Arabic (the owner's Iraqi dialect).",
        "question": "Reframe to end with a genuine question to the niche.",
        "story": "Reframe as a 3-line personal micro-story.",
    }
    try:
        raw = llm_chat(
            cfg.llm,
            system='You rewrite X posts for the same account owner. Output '
                   'STRICT JSON: {"tweet": "..."} or {"thread": ["...", ...]}'
                   ' for thread mode. Stay in the owner\'s voice.' +
                   voice_mod.voice_prompt_block(lang),
            user=f"MODE: {directives[mode]}\n\nORIGINAL:\n{d['text'][:1000]}"
                 "\n\nRewrite now.",
            temperature=0.8, json_mode=True)
        data = extract_json(raw)
    except LLMError as e:
        return {"ok": False, "error": f"rewrite failed: {e}"}
    if mode == "thread" and data.get("thread"):
        thread = [scrub(str(t)) for t in data["thread"] if str(t).strip()]
        if not thread or any(len(t) > 280 for t in thread):
            return {"ok": False, "error": "thread rewrite invalid"}
        did = db.add_draft(text=thread[0], thread=thread, kind="post",
                           temperature="bold",
                           meta={"source": "remix", "remix_of": draft_id,
                                 "mode": mode})
        return {"ok": True, "draft_id": did, "thread_tweets": len(thread)}
    text = scrub(str(data.get("tweet") or ""))
    if not text:
        return {"ok": False, "error": "rewrite came back empty"}
    did = db.add_draft(text=text, kind="post", temperature="bold",
                       meta={"source": "remix", "remix_of": draft_id,
                             "mode": mode})
    return {"ok": True, "draft_id": did, "text": text[:200]}


def draft_variants(cfg: Config, text: str = "", count: int = 3,
                   topic: str = "") -> dict:
    """N takes on one idea, all saved as drafts — approve one, delete rest."""
    from .llm import chat as llm_chat, extract_json, LLMError
    from . import voice as voice_mod
    from ..core.text import scrub_ai_punctuation as scrub
    src = (text or topic or "").strip()
    if not src:
        return {"ok": False, "error": "text or topic required"}
    n = max(2, min(int(count), 4))
    try:
        raw = llm_chat(
            cfg.llm,
            system='You write X post variants for the same owner. Output '
                   'STRICT JSON: {"variants": ["v1", "v2", ...]} with '
                   f'exactly {n} variants, each a DIFFERENT angle/format, '
                   "all in the owner's voice." +
                   voice_mod.voice_prompt_block(),
            user=f"IDEA:\n{src[:800]}\n\nWrite {n} variants.",
            temperature=0.9, json_mode=True)
        data = extract_json(raw)
    except LLMError as e:
        return {"ok": False, "error": f"variants failed: {e}"}
    variants = [scrub(str(v)) for v in (data.get("variants") or [])
                if str(v).strip()]
    ids = [db.add_draft(text=v, kind="post", temperature="bold",
                        meta={"source": "variants"}) for v in variants]
    if not ids:
        return {"ok": False, "error": "no valid variants returned"}
    return {"ok": True, "draft_ids": ids, "count": len(ids)}


def get_digest(cfg: Config) -> dict:
    """Today's full digest on demand."""
    from . import digest as digest_mod
    from datetime import date
    d = digest_mod.build_digest(cfg, date.today().isoformat())
    did = d.did if hasattr(d, "did") else {}
    return {"ok": True, "date": date.today().isoformat(), "digest": did}


def reply_to_mention(cfg: Config, mention: str = "") -> dict:
    """Draft a reply to a specific mention (tweet id); without args, list
    pending mentions."""
    from . import mentions as mentions_mod
    target = mention.strip()
    if not target:
        rows = mentions_mod.pending_mentions(limit=5)
        if not rows:
            return {"ok": True, "pending": 0,
                    "note": "no unhandled mentions right now"}
        return {"ok": True, "pending": len(rows),
                "mentions": [{"x_id": r["x_id"],
                              "author": r.get("author_handle"),
                              "text": (r.get("text") or "")[:100]}
                             for r in rows]}
    with db.connect() as c:
        row = c.execute(
            "SELECT * FROM seen_mentions WHERE x_id=? AND account_id=?",
            (target, db.active_account())).fetchone()
    if not row:
        return {"ok": False, "error": f"no seen mention {target!r} — call "
                "without args to see pending ids"}
    did = mentions_mod.draft_mention_reply(cfg, dict(row))
    if not did:
        return {"ok": False, "error": "draft failed — see agent log"}
    return {"ok": True, "draft_id": did}


def competitor_scan(cfg: Config, handle: str = "") -> dict:
    """Study a competitor's timeline: what works for them. Read-only."""
    from . import brain as brain_mod
    from ..x.client import build_client
    import asyncio
    h = handle.strip().lstrip("@")
    if not h:
        return {"ok": False, "error": "handle required"}

    async def _pull():
        x = build_client(cfg)
        return await x.user_tweets(h, limit=30)
    try:
        posts = asyncio.run(_pull())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"pull failed: {e}"}
    if not posts:
        return {"ok": False, "error": f"@{h} returned no posts"}
    ranked = sorted(posts, key=lambda p: (p.get("likes") or 0) +
                    2 * (p.get("reposts") or 0), reverse=True)
    top = [{"likes": p.get("likes"), "text": (p.get("text") or "")[:140]}
           for p in ranked[:5]]
    try:
        brain_mod.journal_append(
            "competitor-scan",
            f"Studied @{h}: top post {ranked[0].get('likes')} likes — "
            f"{(ranked[0].get('text') or '')[:80]}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "handle": h, "posts_studied": len(posts),
            "top_posts": top,
            "note": f"Belongs to @{h} — extract the PATTERN, never copy."}


def reflect_now(cfg: Config, trigger: str = "chat",
                feedback: str = "") -> dict:
    """Learn RIGHT NOW from recent chats/rejections instead of waiting for
    the scheduled reflection."""
    from . import brain as brain_mod
    from .llm import LLMError
    valid = ("chat", "learn", "metrics")
    if trigger not in valid:
        return {"ok": False, "error": f"trigger must be one of {valid}"}
    payload = {"note": feedback[:500]} if feedback.strip() else None
    try:
        res = brain_mod.reflect(cfg, trigger, payload=payload)
        a = res.get("applied", {})
        return {"ok": True, "trigger": trigger,
                "rules_added": len(a.get("added_rules", [])),
                "rules_retired": len(a.get("retired_rules", []))}
    except LLMError as e:
        return {"ok": False, "error": f"reflection failed: {e}"}


def register_all() -> None:
    register("remix_draft", remix_draft)
    register("draft_variants", draft_variants)
    register("get_digest", get_digest)
    register("reply_to_mention", reply_to_mention)
    register("competitor_scan", competitor_scan)
    register("reflect_now", reflect_now)


def watch_topic(cfg: Config, topic: str = "", action: str = "add") -> dict:
    """Standing trend watch: 'tell me when X trends'. add|list|remove."""
    from . import watch as watch_mod
    if action == "list" or not topic:
        return {"ok": True, "watches": watch_mod.list_watches()}
    if action == "remove":
        return watch_mod.remove_watch(topic)
    return watch_mod.add_watch(topic)


def continue_thread(cfg: Config, draft_id: int = 0,
                    addition: str = "") -> dict:
    """Reply to your own published thread while it's warm."""
    from . import watch as watch_mod
    return watch_mod.continue_thread(cfg, draft_id, addition)


register("watch_topic", watch_topic)
register("continue_thread", continue_thread)
