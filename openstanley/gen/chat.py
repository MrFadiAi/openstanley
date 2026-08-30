"""OpenStanley chat agent — the "AI Head of Content" conversation layer.

v0.3: bilingual (replies in the user's language, drafts in either), tool-calls
(the LLM emits action blocks the backend executes), and streaming
(chat_reply_stream yields token events for SSE).
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Iterator, Optional

from ..core import db
from ..core.config import Config
from ..system import watchdog
from . import brain as brain_mod
from . import instructions as instr_mod
from . import tools as tools_mod
from . import voice_lock
from .algorithm import score_draft
from .lang import detect, reply_language_instruction
from .llm import chat as llm_chat, chat_stream as llm_chat_stream, LLMError
from .style_scan import voice_match

# Hermes-grade agent discipline (adapted from the Hermes agent's prompt;
# GLM is on its list of models that need this enforced explicitly).
AGENT_DISCIPLINE = """# Agent discipline
You are an AGENT, not a narrator. Never end a turn with a promise of future
action ('let me check', 'I will draft it') — execute it in the SAME response:
emit the tool call, or deliver the finished thing. Every response must either
(a) contain tool actions that make progress, or (b) hand the user a finished
result. Process narration ('on it', 'pulling it now') without the deliverable
in the same message is a failure. The deliverable is the actual post text in
a quote block, with its draft id and what happens next — never a description
of a post you are about to write. If several independent lookups are needed
(search, read, list), emit them together in one response. If a tool fails,
say so plainly and take the next real path — never fabricate what you could
not fetch."""

AGENT_DISCIPLINE += """
X LIMIT: a single post is AT MOST 280 characters - count before saving. If the
content runs longer, write it as a THREAD (2-3 tweets, each under 250) and save
with the thread structure, never one oversized post (live 2026-08-29: two
approved posts died at publish with X error 186, silently)."""

AGENT_DISCIPLINE += """
CLOSE THE LOOP: when your investigation finds a problem or an answer, the
same response ends with the disposition — the fix applied (when it is
safe and reversible), or exactly one clear question for the owner to
decide. Never stop at a diagnosis. A reply that ends describing an issue
without resolving it or asking for the decision is incomplete."""

AGENT_DISCIPLINE_TG = AGENT_DISCIPLINE + """

TELEGRAM DELIVERY: when you draft a post the user asked for, the FULL text
appears in your message inside a quote block — never only a draft id or a
summary of what it says. Bold the key terms, use short lists for collections.
One message = one delivered thing."""

SYSTEM = """You are OpenStanley — the user's AI Head of Content for X (Twitter).
You run their entire content operation: study their niche, plan, draft in their
voice, grow their following. You are NOT a generic assistant.

YOUR KNOWLEDGE (fresh context injected below):
- Account: username, followers, recent metrics
- Voice + measured style profile: rubric, examples, punctuation/emoji habits
- Idea bank: scored content angles ready to use
- Drafts: what's pending / scheduled / recently published
- Strategy: their positioning and content pillars (if set)

HOW YOU BEHAVE (like getstanley.ai):
- Proactive, opinionated, brief. Talk like a sharp colleague, not a chatbot.
- When asked to write: produce the actual post(s) in the user's voice, ready
  to approve — put each post candidate in a markdown quote block (> like this)
  so the user can save it as a draft with one click.
- When asked for strategy: give concrete, specific advice tied to their real data.
- Suggest next actions (approve drafts, refresh ideas, engage replies).
- Never claim to have posted anything — publishing requires explicit user
  approval. schedule_draft queues it; the calendar + approval own the rest.
- If account is in dry-run mode and user asks about posting, remind them
  nothing real goes out.
- LANGUAGE: mirror the user. If they write Arabic, reply in Arabic. If they
  mix, mix. When writing a post, keep the post itself in the language they
  asked for (default: the language of their message).

{tools}

Keep replies short and scannable. No filler, no disclaimers, no markdown
headers unless showing a drafted post (then use a quote block).

""" + AGENT_DISCIPLINE

# The Telegram surface's persona. The dashboard prompt above is a WRITE
# assistant: its "draft in the user's X voice" instruction leaks post-style
# quirks (lowercase prose, quirk/typo imitation) into plain conversations.
# Same brain, different voice: the chat is a clean assistant's; the X voice
# lives only inside quoted post candidates.
SYSTEM_TG = """You are OpenStanley — the user's AI Head of Content, talking with
them one-on-one in a private Telegram chat.

Same brain as the dashboard: account, voice rubric, style profile, idea
bank, drafts, strategy, recent metrics (injected below) — and the same
tools.

HOW YOU WRITE HERE — this chat is a conversation, not a post:
- Speak as a clean, warm, direct assistant: proper casing and punctuation,
  concise paragraphs. Telegram messages: short paragraphs, bold for key
  terms, bullet lists for collections. No tables, no headers, no
  horizontal rules — Telegram cannot render them.
- The X-post voice — lowercase prose, stylized misspellings, post-style
  punctuation — belongs only inside post drafts, never in the conversation
  around them. Do not imitate typos or post quirks in your replies.
- The voice tuning knobs shape the POSTS you draft, not the chat surface;
  the chat stays clear and professional while mirroring the user's language.
- When asked to write a post: give the candidate in a markdown quote block
  (> like this), written in the user's real X voice — casing and quirks
  exactly as the post should appear.
- Proactive, opinionated, brief. Suggest the next action. Never claim to
  have posted anything — publishing needs explicit approval.

{tools}

Keep replies short and scannable. No filler, no disclaimers.

""" + AGENT_DISCIPLINE_TG

FOLLOWUP_MARKER = "TOOL RESULTS FOLLOW-UP"


def _build_context(cfg: Config) -> str:
    return _context_trace(cfg)["text"]


def _context_trace(cfg: Config) -> dict:
    """Build the LLM context AND the UI-visible trace of how it was built.

    Returns {"text": prompt context, "steps": Thinking rows,
             "chunks": ContextCards entries}. Steps/chunks only include
    sources that actually exist so the UI never renders empty trace rows.
    """
    me = db.get_me()
    parts = [f"ACCOUNT: @{me.get('username', cfg.x.username or 'unknown')} "
             f"({me.get('followers', '?')} followers, mode={cfg.x.mode})"]
    steps = [{"id": "account", "primary": "Reading your account",
              "secondary": f"@{me.get('username', cfg.x.username or 'unknown')}"}]
    chunks: list[dict] = []

    vp = db.get_setting("voice_profile")
    if vp:
        parts.append("VOICE RUBRIC:\n" + str(vp.get("rubric", vp))[:600])
        steps.append({"id": "voice", "primary": "Checking your voice rubric",
                      "secondary": "profile"})
        chunks.append({"title": "Voice rubric", "body": str(vp.get("rubric", vp))[:200],
                       "source": "voice profile", "badge": "VOICE", "relevance": 94})

    profile = db.get_acct_setting("style_profile")
    if profile:
        stats = profile.get("stats") or {}
        parts.append(
            f"STYLE PROFILE: languages={stats.get('language_mix')}, "
            f"avg_len={stats.get('avg_length_chars')} chars, "
            f"emoji/post={stats.get('emoji', {}).get('per_post')}, "
            f"best_hours={stats.get('posting_times', {}).get('best_hours')}\n"
            + (profile.get("human_summary") or "")[:400])
        scanned = stats.get("posts_scanned") or 0
        steps.append({"id": "style", "primary": "Measuring style profile",
                      "secondary": f"{scanned} posts" if scanned else "scan"})
        if profile.get("human_summary"):
            chunks.append({"title": "Style fingerprint",
                           "body": str(profile["human_summary"])[:200],
                           "source": "deep scan", "badge": "SCAN", "relevance": 90})

    ideas = db.fresh_ideas(6)
    if ideas:
        lines = [f"- {i['title']} (score {i['score']})" for i in ideas]
        parts.append("IDEA BANK TOP:\n" + "\n".join(lines))
        steps.append({"id": "ideas", "primary": "Scanning the idea bank",
                      "secondary": f"top {len(ideas)}"})
        chunks.append({"title": "Idea bank — top angles",
                       "body": " · ".join(i["title"] for i in ideas[:3])[:200],
                       "source": "study loop", "badge": "IDEAS", "relevance": 82})

    d_pending = db.drafts_by_status("draft", 5)
    d_sched = db.drafts_by_status("approved", 5)
    if d_pending:
        parts.append("PENDING DRAFTS:\n" + "\n".join(f"- [{d['id']}] {d['text'][:70]}" for d in d_pending))
    if d_sched:
        parts.append("SCHEDULED:\n" + "\n".join(f"- [{d['id']}] {d['scheduled_at']} {d['text'][:50]}" for d in d_sched))
    if d_pending or d_sched:
        steps.append({"id": "drafts", "primary": "Reviewing drafts + calendar",
                      "secondary": f"{len(d_pending)}+{len(d_sched)}"})

    strategy = db.get_acct_setting("strategy")
    if strategy:
        parts.append("STRATEGY (summary):\n" + json.dumps(strategy, ensure_ascii=False)[:400])
        steps.append({"id": "strategy", "primary": "Loading your strategy",
                      "secondary": "pillars"})
        chunks.append({"title": "Strategy one-pager",
                       "body": (strategy.get("text") if isinstance(strategy, dict) else str(strategy)
                                or "")[:200] or "strategy set",
                       "source": "strategy", "badge": "STRAT", "relevance": 76})

    own = db.own_posts(3)
    if own:
        parts.append("RECENT OWN POSTS:\n" + "\n".join(f"- {p['text'][:60]} (♥{p['likes']})" for p in own))
        steps.append({"id": "analytics", "primary": "Reading recent post metrics",
                      "secondary": f"{len(own)} posts"})

    return {"text": "\n\n".join(parts), "steps": steps, "chunks": chunks}


# ---------- voice fine-tuning (FineTuneCard → generation params) ----------

TEMPERATURES = {"safe": 0.4, "bold": 0.7, "experimental": 0.95}


def _voice_tune() -> dict:
    return {
        "temperature": db.get_setting("voice_temperature", "bold"),
        "formality": db.get_setting("voice_formality", 50),
        "lang_mix": db.get_setting("voice_lang_mix", 50),
        "emoji_density": db.get_setting("voice_emoji_density", 3),
    }


def _tune_prompt(tune: dict) -> str:
    """Fine-tune knobs → concrete writing instructions for the system prompt."""
    formality = tune.get("formality", 50)
    lang_mix = tune.get("lang_mix", 50)
    emoji = tune.get("emoji_density", 3)
    lines = ["VOICE TUNING (user-set, obey):"]
    if formality >= 70:
        lines.append("- formality: polished, complete sentences, no slang")
    elif formality <= 30:
        lines.append("- formality: very casual, lowercase energy, slang ok")
    else:
        lines.append("- formality: natural mix of casual and composed")
    if lang_mix >= 70:
        lines.append("- language mix: strongly prefer Arabic (keep technical terms English)")
    elif lang_mix <= 30:
        lines.append("- language mix: strongly prefer English")
    else:
        lines.append("- language mix: comfortable Arabic/English code-switching")
    if emoji >= 6:
        lines.append(f"- emoji: liberal, ~{emoji} per post")
    elif emoji == 0:
        lines.append("- emoji: none, never")
    else:
        lines.append(f"- emoji: sparse, max {emoji} per post")
    return "\n".join(lines)


def _system(cfg: Config, user_message: str) -> str:
    tune = _voice_tune()
    return (brain_mod.brain_context() + "\n\n"
            + SYSTEM.replace("{tools}", tools_mod.TOOLS_PROMPT)
            + "\n\n" + reply_language_instruction(user_message)
            + "\n\n" + _tune_prompt(tune)
            + "\n\n=== CONTEXT ===\n" + _build_context(cfg))


def _system_tg(cfg: Config, user_message: str) -> str:
    """The Telegram surface's persona: identical brain (context, tools,
    tuning), SYSTEM_TG instead of SYSTEM — the conversation is a clean
    assistant's; the X voice stays inside quoted post candidates."""
    tune = _voice_tune()
    return (brain_mod.brain_context() + "\n\n"
            + (SYSTEM_TG + chr(10) * 2 + AGENT_DISCIPLINE_TG).replace("{tools}", tools_mod.TOOLS_PROMPT)
            + "\n\n" + reply_language_instruction(user_message)
            + "\n\n" + _tune_prompt(tune)
            + "\n\n=== CONTEXT ===\n" + _build_context(cfg))


def _llm_temperature() -> float:
    """User temperature ladder (FineTuneCard) → sampling temperature."""
    return TEMPERATURES.get(str(_voice_tune()["temperature"]), 0.7)


def _history_turn(user_message: str) -> str:
    hist = db.chat_history(limit=12)[:-1]
    if not hist:
        return user_message
    hist_text = "\n".join(f"{h['role'].upper()}: {h['content'][:400]}" for h in hist)
    return f"(conversation so far)\n{hist_text}\n\n(user) {user_message}"


def _extract_candidates(reply: str, cfg: Config) -> list[dict]:
    """Markdown quote blocks = post candidates. Each gets an algorithm score
    and a voice-lock check (the Write page shows the voice chip on it)."""
    candidates = []
    for block in re.findall(r"^\s*>[ \t]?(.+)$", reply, re.MULTILINE):
        text = block.strip()
        if len(text) < 15:
            continue
        alg = score_draft(text)
        cand = {"text": text, "alg": alg,
                "language": detect(text),
                "voice_match": voice_match(text)}
        try:  # the lock must never break chat — worst case: no chip
            vc = voice_lock.check_draft(cfg, text)
            if vc.fixed_text:  # the rewrite won → show the fixed text
                cand["text"] = vc.fixed_text
            cand["voice"] = vc.meta()
        except Exception as e:  # noqa: BLE001
            db.log("voice", f"chat candidate check failed: {e}", level="warn")
        candidates.append(cand)
    return candidates[:4]


def _intent_actions(reply: str, cfg: Config) -> list[dict]:
    actions = []
    low = reply.lower()
    if any(w in low for w in ("draft", "post", "write", "منشور", "اكتب")):
        actions.append({"id": "create", "label": "Run create loop"})
    if "idea" in low or "فكرة" in low:
        actions.append({"id": "study", "label": "Refresh ideas"})
    if any(w in low for w in ("reply", "mention", "engage", "رد")):
        actions.append({"id": "engage", "label": "Check mentions"})
    return actions


def _run_tools(cfg: Config, reply: str, max_rounds: int = 3) -> tuple[str, list[dict]]:
    """Hermes-grade agentic loop: execute tool actions, feed results back for
    a follow-up turn, and KEEP GOING while the model chains more actions —
    search → read → draft happens in one conversation turn (bounded at
    max_rounds so a confused model can never loop forever). Returns the
    final prose (with each round's follow-up folded in) + all tool results."""
    results: list[dict] = []
    clean = reply
    seen_actions: set[tuple] = set()
    for _round in range(max_rounds):
        actions = tools_mod.parse_actions(clean)
        if not actions:
            break
        # skip exact repeats (a re-emitted identical action adds nothing)
        actions = [a for a in actions
                   if (a["tool"], json.dumps(a["args"], sort_keys=True))
                   not in seen_actions]
        if not actions:
            break
        for act in actions:
            res = tools_mod.execute_tool(cfg, act["tool"], act["args"])
            results.append({"name": act["tool"], "args": act["args"], **res})
            seen_actions.add((act["tool"],
                             json.dumps(act["args"], sort_keys=True)))
            db.log("chat", f"tool {act['tool']} → ok={res.get('ok')}"
                           + ("" if res.get("ok") else
                              f" error={res.get('error', '')[:150]}"))
        extra = _followup(cfg, clean, results[-len(actions):])
        if extra:
            clean = extra  # the follow-up may chain the next action
        else:
            break
    return clean, results


def _followup(cfg: Config, reply: str, tool_results: list[dict],
               orig_request: str = "") -> str:
    """Feed tool results back → one short LLM turn that folds them into prose."""
    if not tool_results:
        return ""
    # DELIVERABLE MODE (live 2026-08-28: 'not long enough, add more details'
    # -> the agent REPORTED the research and never wrote the post): when the
    # original ask was a draft/edit, this turn WRITES THE POST grounded in
    # the tool results — report mode stays for informational asks
    if looks_like_draft_request(orig_request or ""):
        ddata = dataclasses.replace(cfg.llm, temperature=0.8, max_tokens=4000)
        duser = ("The user's request:" + chr(10) + orig_request[:600] + chr(10) * 2
                 + "Your earlier reply:" + chr(10) + reply[:400] + chr(10) * 2
                 + "The tools ran with REAL results (your research):" + chr(10)
                 + json.dumps(tool_results, ensure_ascii=False, default=str)[:2500]
                 + chr(10) * 2
                 + "The user is waiting for THE POST. Write it NOW as one "
                   "markdown quote block (> ...), fully grounded in the real "
                   "results above, in the owner's voice and dialect, at the "
                   "length the user asked for. No preamble, no report of what "
                   "you found — the quote block IS the reply.")
        try:
            return llm_chat(ddata, system="You are OpenStanley. TOOL RESULTS "
                                          "FOLLOW-UP: deliver the post now — "
                                          "quote block, no report.", user=duser)
        except LLMError:
            return ""
    # SHOW MODE (live 2026-08-30 03:59: 'show me the scheduled posts' got
    # a COUNT again — '40 items total, next up is...' — because the report
    # mode summarizes instead of rendering. When the owner asked to
    # SEE/LIST items and the tool returned them, this turn RENDERS THE
    # ITEMS, not a summary of them.)
    if _wants_items_rendered(orig_request or ""):
        sdata = dataclasses.replace(cfg.llm, temperature=0.4, max_tokens=4000)
        suser = ("The user asked:" + chr(10) + orig_request[:400] + chr(10) * 2
                 + "The tools ran with REAL results:" + chr(10)
                 + json.dumps(tool_results, ensure_ascii=False, default=str)[:3500]
                 + chr(10) * 2
                 + "The user asked to SEE these items. RENDER THEM in your "
                   "reply as a formatted list — each item with its id, time "
                   "(if any), and full text. A count or summary of the items "
                   "is NOT showing them. Render every item the tool returned, "
                   "most relevant first. Same language as your reply.")
        try:
            return llm_chat(sdata, system="You are OpenStanley. TOOL RESULTS "
                                          "FOLLOW-UP: render the items the "
                                          "owner asked to see, as a list.",
                            user=suser)
        except LLMError:
            pass  # fall through to report mode
    data = dataclasses.replace(cfg.llm, temperature=0.4, max_tokens=500)
    user = (f"Your reply was:\n{reply[:800]}\n\nThe tools executed with REAL "
            f"results:\n{json.dumps(tool_results, ensure_ascii=False, default=str)[:1500]}\n\n"
            "Write 1-3 short sentences reporting what happened (real numbers "
            "only, same language as your reply). No new actions.")
    try:
        return llm_chat(data, system=f"You are OpenStanley. {FOLLOWUP_MARKER}: "
                                      "report tool results tersely.", user=user)
    except LLMError:
        return ""


def _wants_items_rendered(message: str) -> bool:
    """The owner asked to SEE/LIST/SHOW items — not a summary of them."""
    m = (message or "").lower()
    show_verbs = ("show me", "show ", "list", "what are", "what is",
                  "which post", "which draft", "where is", "display",
                  "ارني", "اكتبلي", "شنو الجدول", "ايش منشور")
    return any(v in m for v in show_verbs)


def chat_reply(cfg: Config, user_message: str, history: Optional[list] = None) -> dict:
    """One-shot OpenStanley reply (non-streaming path). Synchronous (LLM call)."""
    db.add_chat_message("user", user_message)
    llm_cfg = dataclasses.replace(cfg.llm, temperature=_llm_temperature(),
                                  max_tokens=4000)  # 1200 starved GLM: thinking ate the whole budget before any text (stop_reason=max_tokens, zero deltas) — the entire 'agent not responding' day was this number
    system = _system(cfg, user_message)
    try:
        reply = llm_chat(llm_cfg, system=system, user=_history_turn(user_message))
        watchdog.note_chat_llm(True)
    except LLMError as e:
        watchdog.note_chat_llm(False, str(e))
        reply = f"(LLM error: {e})"

    clean, tool_results = _run_tools(cfg, reply)
    clean = tools_mod.strip_actions(reply)
    if tool_results:
        extra = _followup(cfg, reply, tool_results, user_message)
        if extra:
            clean += "\n\n" + extra
    # instruction memory: a directive-shaped message becomes a standing
    # brain rule NOW, not whenever the next reflect("chat") might catch it
    captured = instr_mod.capture(cfg, user_message)
    if captured:
        clean += "\n\n" + instr_mod.ack_line(captured)
    db.add_chat_message("assistant", clean,
                        meta={"tool_results": tool_results})
    brain_mod.maybe_reflect_chat_async(cfg)  # every 10th message → reflect
    candidates = _extract_candidates(clean, cfg)
    trace = _context_trace(cfg)
    return {"reply": clean, "actions": _intent_actions(clean, cfg),
            "tool_results": tool_results, "candidates": candidates,
            "thinking_steps": trace["steps"], "context_chunks": trace["chunks"]}


def chat_reply_stream(cfg: Config, user_message: str) -> Iterator[dict]:
    """Streaming OpenStanley reply — yields event dicts for the SSE endpoint.

    Events: {"type":"thinking_steps", steps, chunks} first, then
    {"type":"token","text":...}, {"type":"tool",name,args,ok,result},
    {"type":"approval",candidate} per post candidate, {"type":"done",...},
    {"type":"error",...}. Save-to-DB happens before done.
    """
    db.add_chat_message("user", user_message)
    trace = _context_trace(cfg)
    yield {"type": "thinking_steps", "steps": trace["steps"],
           "chunks": trace["chunks"]}
    llm_cfg = dataclasses.replace(cfg.llm, temperature=_llm_temperature(),
                                  max_tokens=4000)  # 1200 starved GLM: thinking ate the whole budget before any text (stop_reason=max_tokens, zero deltas) — the entire 'agent not responding' day was this number
    system = _system(cfg, user_message)
    full = []
    try:
        for tok in llm_chat_stream(llm_cfg, system=system, user=_history_turn(user_message)):
            full.append(tok)
            yield {"type": "token", "text": tok}
        watchdog.note_chat_llm(True)
    except LLMError as e:
        watchdog.note_chat_llm(False, str(e))
        yield {"type": "error", "message": str(e)}
        return
    reply = "".join(full)

    # tools + follow-up run AFTER the stream so tokens land fast
    clean, tool_results = _run_tools(cfg, reply)
    clean = tools_mod.strip_actions(reply)
    for res in tool_results:
        yield {"type": "tool", "name": res["name"], "args": res.get("args"),
               "ok": bool(res.get("ok")), "result": res}
    if tool_results:
        extra = _followup(cfg, reply, tool_results, user_message)
        if extra:
            clean += "\n\n" + extra
            yield {"type": "token", "text": "\n\n" + extra}

    # instruction memory: capture runs after the tokens landed (the model's
    # own reply is never delayed by it) — the ack rides the final text
    captured = instr_mod.capture(cfg, user_message)
    if captured:
        clean += "\n\n" + instr_mod.ack_line(captured)
    reply_id = db.add_chat_message("assistant", clean,
                                   meta={"tool_results": tool_results})
    brain_mod.maybe_reflect_chat_async(cfg)  # every 10th message → reflect
    candidates = _extract_candidates(clean, cfg)
    for cand in candidates:
        yield {"type": "approval", "candidate": cand}
    yield {"type": "done", "reply_id": reply_id, "reply": clean,
           "actions": _intent_actions(clean, cfg),
           "tool_results": tool_results,
           "candidates": candidates}


DRAFT_INTENT_RE = re.compile(
    "(?i)" + chr(92) + "b(draft|write|compose|thread)" + chr(92) + "b"
    "|اكتب|صيغ|منشور")


def looks_like_draft_request(message: str) -> bool:
    """The user is asking for a post to be written (not just chatting)."""
    return bool(DRAFT_INTENT_RE.search(message or ""))


def force_post_candidate(cfg: Config, user_message: str) -> Optional[str]:
    """GUARANTEED-DRAFT nudge: the agentic chain sometimes researches
    (web_read/github pulls) and ends its turn WITHOUT ever emitting the
    post — the owner asked, got 'let me pull the details first', and no
    draft materialized (live 2026-08-28 15:53). One forced turn: write the
    post NOW as a quote block. Returns the quote text or None."""
    dataclasses_replace = dataclasses.replace(cfg.llm, temperature=0.7,
                                              max_tokens=600)
    try:
        raw = llm_chat(dataclasses_replace,
                       system=_system_tg(cfg, user_message) + chr(10) * 2 +
                              "The conversation above researched the topic but "
                              "NO post was delivered. Write the post NOW — "
                              "exactly ONE markdown quote block (> like this), "
                              "in the owner's X voice, ready to approve. No "
                              "preamble, no apology, just the quote block.",
                       user=f"(original request) {user_message[:600]}")
    except LLMError:
        return None
    for block in re.findall(r"^\s*>[ 	]?(.+)$", raw, re.MULTILINE):
        text = block.strip()
        if len(text) >= 15:
            return text
    return None


def draft_from_chat(cfg: Config, text: str, image: str | None = None) -> int:
    """User approved a post written in chat → save as a real draft for the queue.

    The human already approved it, so the voice lock never rejects here —
    it only attaches the score (the Inbox chip shows the verdict).
    """
    # watchdog burst guard: a confused reply storm must not be able to fill
    # the queue — returns -1 (caller surfaces "not saved") when tripped
    if not watchdog.allow_chat_draft():
        db.log("chat", "chat draft save BLOCKED by watchdog burst guard",
               level="warn")
        return -1
    alg = score_draft(text)
    meta = {"source": "chat", "via": "openstanley-chat",
            "language": detect(text), "alg": alg,
            "voice_match": voice_match(text)}
    try:
        meta["voice"] = voice_lock.check_draft(cfg, text,
                                               allow_fix=False).meta()
    except Exception as e:  # noqa: BLE001
        db.log("voice", f"chat draft check failed: {e}", level="warn")
    did = db.add_draft(text=text, kind="post", temperature="chat",
                       meta=meta, image=image)
    watchdog.note_chat_draft()
    db.log("chat", f"chat draft saved #{did} (alg {alg['score']})")
    return did
