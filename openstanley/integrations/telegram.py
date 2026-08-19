"""Telegram frontend (v0.4.4) — reach OpenStanley from a chat app.

The dashboard stays primary; Telegram is a SECOND frontend that mirrors and
notifies: talk to OpenStanley (same chat engine, per-chat sessions), approve or
reject drafts, read status/ideas/digest, and receive the daily digest plus
"needs approval" cards the moment a loop drafts something.

Design constraints (from the brief):
  * works WITHOUT a token configured (disabled state) — activates when the
    settings arrive; no webhook infra, raw long-polling against
    api.telegram.org via httpx (python-telegram-bot is banned)
  * allowed chat ids gate everything: strangers get ONE polite refusal then
    silence; an empty list replies "set your chat id <id>" (bootstrap UX)
  * TG can approve EXISTING drafts but nothing auto-publishes — the human
    approval gate applies exactly as on the dashboard
  * the token is a secret: masked in GET /api/settings, never logged
  * outbound is rate-limited (20 msgs/min) — overflow drops with a warn log

All HTTP goes through module-level `httpx` calls, so tests fake the module
attribute the same way the digest tests fake theirs — zero real traffic.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import httpx

from ..core import db
from ..core.config import Config

API_URL = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_S = 25          # Telegram long-poll window
HTTP_TIMEOUT_S = 10.0        # sendMessage / getUpdates client timeout
EMPTY_POLL_SLEEP_S = 0.5     # breather between empty long-polls
ERROR_SLEEP_S = 5.0          # backoff after a failed poll
MAX_OUT_PER_MIN = 20         # outbound rate limit (drops beyond this)
SESSION_CAP = 20             # messages remembered per TG chat
DRAFTS_PAGE = 5              # /drafts previews
IDEAS_PAGE = 5               # /ideas rows
PREVIEW_CHARS = 90
MSG_LIMIT = 4000             # Telegram hard limit is 4096 — clip under it
TOKEN_MIN_LEN = 10           # anything shorter is not a real bot token

HELP_TEXT = (
    "I'm OpenStanley — your AI Head of Content, now on Telegram.\n\n"
    "/status — identity, autopilot, health, bank, today's caps\n"
    "/ideas — top idea-bank angles\n"
    "/drafts — drafts waiting for your approval\n"
    "/approve <id> — approve a draft (it gets scheduled)\n"
    "/reject <id> — reject a draft\n"
    "/post <text> — save your own text as a draft for review\n"
    "/digest — today's report, on demand\n"
    "/study — study your X account fully & refresh my brain\n"
    "\nAnything else you type, I answer — same brain as the dashboard. Ask me\n"
    "to write a post and I'll save it as a draft for your /approve."
)

# module state — single poller per process, like autopilot's module state
_state: dict = {
    "task": None,            # asyncio.Task running the poll loop
    "offset": 0,             # last processed update_id + 1
    "mode": "disabled",      # disabled | polling | bad_token
    "stop": threading.Event(),
}
_denied_chats: set[int] = set()          # strangers already refused once
_sessions: dict[int, list[dict]] = {}    # chat id → [{role, content}, …]
_rate_times: deque[float] = deque()      # send timestamps (rate limiter)


# ---------------- settings ----------------

def bot_token() -> str:
    return str(db.get_setting("tg_bot_token") or "")


def allowed_chats() -> list[int]:
    """Configured chat ids — accepts a list or a comma string, sorted."""
    raw = db.get_setting("tg_allowed_chats")
    items: list = raw if isinstance(raw, list) else str(raw or "").split(",")
    out = set()
    for item in items:
        try:
            out.add(int(str(item).strip()))
        except ValueError:
            continue
    return sorted(out)


def is_enabled() -> bool:
    return bool(db.get_setting("tg_enabled")) and len(bot_token()) >= TOKEN_MIN_LEN


def mask_token(token: str) -> str:
    """Never return or log the token — show only its last 4 chars."""
    return f"••••{token[-4:]}" if len(token) > 8 else ("••••" if token else "")


# ---------------- transport (sync httpx — faked at this seam in tests) ----------------

def _api(token: str, method: str, params: dict) -> httpx.Response:
    """One Bot API call. Raw httpx — no SDK, no session, no retry."""
    return httpx.post(API_URL.format(token=token, method=method), json=params,
                      timeout=HTTP_TIMEOUT_S)


def _get_updates(token: str, offset: int) -> httpx.Response:
    return httpx.post(API_URL.format(token=token, method="getUpdates"),
                      json={"offset": offset, "timeout": POLL_TIMEOUT_S,
                            "allowed_updates": ["message"]},
                      timeout=POLL_TIMEOUT_S + HTTP_TIMEOUT_S)


def _entities_rejected(r: httpx.Response) -> bool:
    """Telegram's 400 for HTML it can't parse ('can't parse entities')."""
    if getattr(r, "status_code", None) != 400:
        return False
    try:
        return "can't parse entities" in str(r.text).lower()
    except Exception:  # noqa: BLE001 — a probe must never raise
        return False


def _api_send_text(token: str, chat_id: int, text: str) -> httpx.Response:
    """sendMessage with HTML formatting; if Telegram rejects the entities,
    retried ONCE with parse_mode removed — formatting must never cost
    delivery."""
    r = _api(token, "sendMessage",
             {"chat_id": chat_id, "text": _format_tg(text), "parse_mode": "HTML"})
    if _entities_rejected(r):
        r = _api(token, "sendMessage", {"chat_id": chat_id, "text": _clip(text)})
    return r


def _api_edit_text(token: str, chat_id: int, message_id: int,
                   text: str) -> httpx.Response:
    """editMessageText with the same formatting + plain-retry contract."""
    r = _api(token, "editMessageText",
             {"chat_id": chat_id, "message_id": message_id,
              "text": _format_tg(text), "parse_mode": "HTML"})
    if _entities_rejected(r):
        r = _api(token, "editMessageText",
                 {"chat_id": chat_id, "message_id": message_id,
                  "text": _clip(text)})
    return r


def _clip(text: str, limit: int = MSG_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------- markdown → Telegram HTML ----------------
#
# Hermes-style rich messages: bold labels, bullets, code, links — not walls of
# emoji. HTML (not MarkdownV2) because it needs no escape-everything traps.
# Contract: whatever the LLM emitted, the output must be something Telegram
# accepts — stray & < > are escaped BEFORE tags are inserted, and unbalanced
# markers stay literal instead of becoming half a tag.

TG_MSG_LIMIT = 4096            # Telegram's hard ceiling for one message

_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\n?(.*?)```", re.DOTALL)
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_ITALIC_RE = re.compile(r"\*(?!\s)([^*\n]+?)(?<!\s)\*")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_MD_QUOTE_RE = re.compile(r"^&gt;[ \t]?")
_MD_BULLET_RE = re.compile(r"^[-*][ \t]+")

_BLOCK_TAG = "\x00B{}\x00"     # placeholder tokens for stashed code (the
_CODE_TAG = "\x00C{}\x00"      # \x00 byte never occurs in real message text)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_to_tg_html(text: str) -> str:
    """Markdown-ish LLM output → Telegram HTML. Converts **bold**, *italic*,
    `code`, fenced ```blocks``` (→ <pre>, language tag dropped), [links](url),
    > quote blocks (→ <blockquote>, post candidates keep their own voice) and
    normalises -/* bullets to the house `·`. Everything else is escaped."""
    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        blocks.append(f"<pre>{_esc(m.group(1))}</pre>")
        return _BLOCK_TAG.format(len(blocks) - 1)

    text = _MD_CODE_BLOCK_RE.sub(_stash_block, text or "")

    codes: list[str] = []

    def _stash_code(m: re.Match) -> str:
        codes.append(f"<code>{_esc(m.group(1))}</code>")
        return _CODE_TAG.format(len(codes) - 1)

    text = _MD_CODE_RE.sub(_stash_code, text)

    text = _esc(text)
    text = _MD_LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _MD_BOLD_RE.sub(r"<b>\1</b>", text)
    text = _MD_ITALIC_RE.sub(r"<i>\1</i>", text)
    for i, c in enumerate(codes):
        text = text.replace(_CODE_TAG.format(i), c)

    # quote lines → one <blockquote> per contiguous group; bullets → `· `
    src = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(src):
        q = _MD_QUOTE_RE.match(src[i])
        if not q:
            out.append(_MD_BULLET_RE.sub("· ", src[i]))
            i += 1
            continue
        group: list[str] = []
        while i < len(src) and (m := _MD_QUOTE_RE.match(src[i])):
            group.append(_MD_BULLET_RE.sub("· ", src[i][m.end():]))
            i += 1
        out.append("<blockquote>" + "\n".join(group) + "</blockquote>")
    text = "\n".join(out)

    for i, b in enumerate(blocks):
        text = text.replace(_BLOCK_TAG.format(i), b)
    return text


def _format_tg(text: str) -> str:
    """One outbound text, send-ready: clipped at the SOURCE (so a clip can
    never cut a tag in half), markdown → HTML, and a plain-text fallback when
    tag overhead would push the message past Telegram's 4096 ceiling."""
    clipped = _clip(text)
    html = _md_to_tg_html(clipped)
    return html if len(html) <= TG_MSG_LIMIT else clipped


def _scrub(text: str, token: str) -> str:
    """Exception strings can embed the request URL — never let the token
    reach a log line."""
    return text.replace(token, "•••") if token else text


def _reset_rate() -> None:
    """Test seam — a fresh rate-limit window."""
    _rate_times.clear()


# ---------------- outbound ----------------

def _rate_allow() -> bool:
    now = time.monotonic()
    while _rate_times and now - _rate_times[0] > 60:
        _rate_times.popleft()
    if len(_rate_times) >= MAX_OUT_PER_MIN:
        return False
    _rate_times.append(now)
    return True


def send_message(chat_id: int, text: str) -> dict:
    """One outbound message. Rate-limited; overflow drops with a warn log.
    Never raises — returns an ok/error record instead."""
    token = bot_token()
    if not token:
        return {"ok": False, "status_code": None, "error": "no bot token"}
    if not _rate_allow():
        db.log("telegram", f"rate limit hit — message to chat {chat_id} dropped",
               level="warn")
        return {"ok": False, "status_code": None, "error": "rate limited"}
    try:
        r = _api_send_text(token, chat_id, text)
        ok = 200 <= r.status_code < 300
        if not ok:
            db.log("telegram", f"sendMessage to chat {chat_id} failed "
                               f"(HTTP {r.status_code})", level="warn")
        return {"ok": ok, "status_code": r.status_code,
                "error": None if ok else r.text[:200]}
    except Exception as e:  # noqa: BLE001 — sending must never take the caller down
        db.log("telegram", f"sendMessage to chat {chat_id} error: "
                           f"{_scrub(str(e), token)}", level="warn")
        return {"ok": False, "status_code": None,
                "error": _scrub(str(e), token)[:200]}


# ---------------- streaming chat ----------------

STREAM_EDIT_MIN_S = 1.2     # throttle: never edit faster than this
STREAM_EDIT_EVERY = 12      # …and at most one edit per N chunk groups
STREAM_TYPING_PING_S = 4.0  # send chatAction "typing" this often while waiting


def send_stream(chat_id: int, text_stream) -> dict:
    """Progressive streaming for TG: sendMessage with the first chunk, then
    editMessageText as more tokens arrive (throttled), final edit = the full
    text. Falls back to one sendMessage if the initial send fails. Never
    raises. text_stream yields str chunks."""
    token = bot_token()
    if not token:
        return {"ok": False, "status_code": None, "error": "no bot token"}
    buf: list[str] = []
    msg_id: int | None = None
    last_edit_t = 0.0
    chunks_since_edit = 0
    last_ping_t = time.time()
    try:
        for chunk in text_stream:
            if not chunk:
                continue
            buf.append(chunk)
            # keep the user informed while the first LLM tokens are slow
            now = time.time()
            if msg_id is None and now - last_ping_t >= STREAM_TYPING_PING_S:
                try:
                    _api(token, "sendChatAction",
                         {"chat_id": chat_id, "action": "typing"})
                except Exception:  # noqa: BLE001 — cosmetic, never fatal
                    pass
                last_ping_t = now
            if msg_id is None and (len("".join(buf)) >= 24 or _stream_done(buf)):
                sent = _send_and_capture(token, chat_id, "".join(buf))
                if sent.get("ok"):
                    msg_id = sent["message_id"]
                    last_edit_t = time.time()
                continue
            if msg_id is None:
                continue
            chunks_since_edit += 1
            now = time.time()
            elapsed = now - last_edit_t
            if (chunks_since_edit >= STREAM_EDIT_EVERY
                    and elapsed >= STREAM_EDIT_MIN_S):
                try:
                    _api_edit_text(token, chat_id, msg_id, "".join(buf))
                    last_edit_t = now
                    chunks_since_edit = 0
                except Exception:  # noqa: BLE001 — a dropped edit is cosmetic
                    pass
        # final edit — always the complete text
        full = "".join(buf)
        if msg_id is None:
            return _send_and_capture(token, chat_id, full)
        try:
            _api_edit_text(token, chat_id, msg_id, full)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "status_code": 200, "message_id": msg_id,
                "error": None}
    except Exception as e:  # noqa: BLE001 — streaming must never raise
        db.log("telegram", f"stream to chat {chat_id} aborted: "
                           f"{_scrub(str(e), token)}", level="warn")
        # still try to deliver whatever we accumulated
        try:
            if buf:
                if msg_id is None:
                    return _send_and_capture(token, chat_id, "".join(buf))
                _api_edit_text(token, chat_id, msg_id, "".join(buf))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "status_code": None,
                "error": _scrub(str(e), token)[:200]}


def _stream_done(buf: list[str]) -> bool:
    """Heuristic: first sentence/line complete → worth showing the bubble."""
    text = "".join(buf)
    return ("\n" in text) or ("." in text[-3:])


def _send_and_capture(token: str, chat_id: int, text: str) -> dict:
    r = _api_send_text(token, chat_id, text)
    ok = 200 <= r.status_code < 300
    mid = None
    if ok:
        try:
            mid = int(r.json().get("result", {}).get("message_id"))
        except Exception:  # noqa: BLE001
            mid = None
    return {"ok": ok, "status_code": r.status_code, "message_id": mid,
            "error": None if ok else r.text[:200]}


def notify(text: str) -> dict:
    """Broadcast to every allowed chat (digest cron + approval cards).
    Rate-limited per message; failures are logged, never raised."""
    chats = allowed_chats()
    if not is_enabled():
        return {"ok": False, "sent": 0, "chats": 0, "error": "telegram disabled"}
    sent = sum(1 for c in chats if send_message(c, text)["ok"])
    return {"ok": sent > 0, "sent": sent, "chats": len(chats), "error": None}


def notify_bg(text: str) -> None:
    """Fire-and-forget notify from sync contexts (agent loops) — a daemon
    thread so a slow HTTP call can never block the caller."""
    threading.Thread(target=notify, args=(text,), daemon=True,
                     name="tg-notify").start()


def notify_new_drafts(draft_ids: list[int]) -> dict:
    """Compact 'needs approval' card for drafts a loop just created."""
    if not draft_ids:
        return {"ok": False, "sent": 0, "chats": 0, "error": "no drafts"}
    rows = [d for d in (db.get_draft(i) for i in draft_ids) if d]
    if not rows:
        return {"ok": False, "sent": 0, "chats": 0, "error": "no drafts"}
    n = len(rows)
    lines = [f"🟡 {n} draft needs your approval:" if n == 1
             else f"🟡 {n} drafts need your approval:"]
    lines += (f"· {draft_line(d, with_kind=True)}" for d in rows[:DRAFTS_PAGE])
    if len(rows) > DRAFTS_PAGE:
        lines.append(f"· +{len(rows) - DRAFTS_PAGE} more — /drafts")
    lines.append("Reply /approve <id> or /reject <id> — or open the dashboard.")
    return notify("\n".join(lines))


# ---------------- inbound parsing + auth ----------------

_CMD_RE = re.compile(r"^/([a-z]+)(?:@\w+)?\s*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_command(text: str) -> Optional[tuple[str, str]]:
    """'/Approve@mybot 12  x' → ('approve', '12  x'). None for plain text."""
    if not text or not text.startswith("/"):
        return None
    m = _CMD_RE.match(text.strip())
    if not m:
        return None
    return m.group(1).lower(), m.group(2).strip()


def _auth_reply(chat_id: int) -> Optional[str]:
    """None when the chat is allowed. "" when silenced. Otherwise the ONE
    refusal strangers get, or the bootstrap hint when no chats are set."""
    chats = allowed_chats()
    if chat_id in chats:
        return None
    if not chats:
        return ("This bot isn't linked to a chat yet. In the dashboard "
                "Settings → Telegram, add your chat id to the allowed list:\n"
                f"tg_allowed_chats = {chat_id}")
    if chat_id in _denied_chats:
        return ""  # already refused once — silence from now on
    _denied_chats.add(chat_id)
    return "This OpenStanley is private — ask its owner to add your chat id."


# ---------------- chat sessions (same engine as the dashboard) ----------------

def _remember(chat_id: int, role: str, content: str) -> None:
    """One turn into the RAM session AND the DB (chat_messages) — TG sessions
    survive restarts, and chat_id keeps them out of the dashboard's history."""
    sess = _sessions.setdefault(chat_id, [])
    sess.append({"role": role, "content": content})
    del sess[:-SESSION_CAP]  # cap the memory the TG chat keeps
    db.add_chat_message(role, content, meta={"chat_id": chat_id}, chat_id=chat_id)


def _history_turn(chat_id: int, user_message: str) -> str:
    """Mirror of chat._history_turn, but over this chat's private session.
    RAM session empty (restart wiped it) → rebuild from the persisted rows."""
    hist = _sessions.get(chat_id, [])[:-1]
    if not hist:
        hist = db.chat_history_for_chat(chat_id, SESSION_CAP + 1)[:-1]
    if not hist:
        return user_message
    hist_text = "\n".join(f"{h['role'].upper()}: {h['content'][:400]}" for h in hist)
    return f"(conversation so far)\n{hist_text}\n\n(user) {user_message}"


def chat_reply_tg_stream(cfg: Config, chat_id: int, user_message: str):
    """Streaming TG chat: the dashboard streaming engine with the TG persona
    (chat_mod._system_tg — clean assistant voice, X voice scoped to quoted
    post candidates), per-chat session, tools, follow-up. Side effects match
    chat_reply_stream: session memory, DB persistence, brain reflect — plus
    post candidates are saved as real drafts (TG has no save button), each
    announced with a /approve line. Publishing still only happens through
    the approval gate."""
    import dataclasses

    from ..gen import brain as brain_mod
    from ..gen import chat as chat_mod
    from ..gen import tools as tools_mod
    from ..gen.llm import LLMError

    _remember(chat_id, "user", user_message)
    llm_cfg = dataclasses.replace(cfg.llm, temperature=chat_mod._llm_temperature(),
                                  max_tokens=1200)
    full: list[str] = []
    try:
        for tok in chat_mod.llm_chat_stream(
                llm_cfg, system=chat_mod._system_tg(cfg, user_message),
                user=_history_turn(chat_id, user_message)):
            full.append(tok)
            yield tok
    except LLMError as e:
        yield f"(LLM error: {e})"
        return

    reply = "".join(full)
    tool_results = chat_mod._run_tools(cfg, reply)
    clean = tools_mod.strip_actions(reply)
    if tool_results:
        extra = chat_mod._followup(cfg, reply, tool_results)
        if extra:  # web parity: real tool results folded into prose
            clean += "\n\n" + extra
        else:      # LLM down → the terse fallback still says what ran
            clean += "\n" + "\n".join(
                f"· {r['name']}: {'ok' if r.get('ok') else 'failed'}"
                for r in tool_results)

    # post candidates (markdown quote blocks) → real drafts, exactly what the
    # dashboard's approval cards hold. Saving is safe; publishing is not ours.
    draft_ids: list[int] = []
    for cand in chat_mod._extract_candidates(clean, cfg):
        try:
            draft_ids.append(chat_mod.draft_from_chat(cfg, cand["text"]))
        except Exception as e:  # noqa: BLE001 — a failed save must not kill the reply
            db.log("telegram", f"chat candidate draft save failed: {e}",
                   level="warn")
    if draft_ids:
        clean += "\n" + "\n".join(
            f"📝 Saved as draft #{d} — /approve {d} to publish" for d in draft_ids)

    _remember(chat_id, "assistant", clean)
    brain_mod.maybe_reflect_chat_async(cfg)  # every 10th message → reflect
    # delta for the final bubble edit: what the streamed text is missing.
    # clean starts at strip_actions(reply), so the delta is the part after it;
    # the already-streamed action fences can't be un-sent (append-only bubble).
    tail = clean[len(tools_mod.strip_actions(reply)):]
    if tail:
        yield tail


# ---------------- commands ----------------

def _voice_chip(meta: dict) -> str:
    v = meta.get("voice") or {}
    if v.get("score") is not None:
        return f"voice {v['score']}%"
    vm = meta.get("voice_match")
    return f"voice {vm}%" if vm is not None else "voice —"


def _target_chip(d: dict) -> str:
    meta = d.get("meta") or {}
    if d.get("kind") == "reply":
        who = meta.get("target_author") or meta.get("author")
        return f"→ @{who}" if who else "→ reply"
    if d.get("kind") == "quote":
        q = meta.get("quote") or {}
        return f"→ quoting @{q.get('author', '?')}"
    return ""


def draft_line(d: dict, with_kind: bool = False) -> str:
    text = " ".join((d.get("text") or "").split())
    text = text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"
    head = f"**#{d['id']}**" + (f" [{d.get('kind') or 'post'}]" if with_kind else "")
    chips = " · ".join(c for c in (_target_chip(d), _voice_chip(d.get("meta") or {}))
                       if c)
    return f"{head} “{text}”{(' · ' + chips) if chips else ''}"


def _cmd_status(cfg: Config) -> str:
    from ..core.safety import usage
    from ..gen import autopilot as ap_mod
    from ..gen import ideas as ideas_mod

    me = db.get_setting("me") or {}
    ap = ap_mod.get_state()
    smoke = db.get_setting("smoke_last") or {}
    bank = ideas_mod.bank_health()
    caps = usage()
    ap_line = (f"· **autopilot** on — phase {ap.get('phase')}, "
               f"next {ap.get('next_tick')}" if ap.get("enabled")
               else "· **autopilot** off")
    bank_line = f"· **idea bank** {bank['count']} idea(s)"
    if (bank.get("last") or {}).get("at"):
        bank_line += f" · replenished {bank['last']['at'][:10]}"
    lines = [
        f"**Status — @{me.get('username', cfg.x.username or 'unknown')}**"
        f" ({me.get('followers', '?')} followers, mode={cfg.x.mode})",
        ap_line,
        f"· **health check** {smoke.get('status', 'never')}",
        bank_line,
        f"· **today** {caps.get('posts', 0)}/{cfg.x.max_posts_per_day} posts"
        f" · {caps.get('replies', 0)}/{cfg.x.max_replies_per_day} replies",
    ]
    return "\n".join(lines)


def _cmd_ideas() -> str:
    ideas = db.fresh_ideas(IDEAS_PAGE)
    if not ideas:
        return "Idea bank is empty — /study refills it."
    return "**Idea bank — top angles**\n" + "\n".join(
        f"· {idea['title']} (score {idea['score']})" for idea in ideas)


def _cmd_drafts() -> str:
    drafts = db.drafts_by_status("draft", DRAFTS_PAGE)
    if not drafts:
        return "Nothing waiting — the approval queue is clear."
    return ("**Waiting for approval**\n"
            + "\n".join(f"· {draft_line(d, with_kind=True)}" for d in drafts)
            + "\n/approve <id> · /reject <id> — or open the dashboard.")


STUDY_LOOPS = ("import", "study", "scan", "learn")
STUDY_LOOP_TIMEOUT_S = 15 * 60  # per-loop ceiling — a hung loop must never park the poller thread


def _cmd_study(cfg: Config) -> str:
    """Full 'learn me' chain: import own+niche posts, study the niche, deep-scan
    voice, refresh metrics, reflect everything into the brain. Read-only on X.
    Reuses the server's loop runner core — same code path as the dashboard
    buttons. One loop failing or timing out marks its line and the chain moves
    on; the ✅ only appears when every loop succeeded."""
    import asyncio

    async def _chain() -> tuple[list[str], bool]:
        from ..server.__main__ import run_loop_core
        lines: list[str] = []
        ok_all = True
        for name in STUDY_LOOPS:
            try:
                res = await asyncio.wait_for(run_loop_core(name),
                                             timeout=STUDY_LOOP_TIMEOUT_S) or {}
            except asyncio.TimeoutError:
                ok_all = False
                lines.append(f"· **{name}** timed out after {STUDY_LOOP_TIMEOUT_S}s")
                continue
            except Exception as e:  # noqa: BLE001 — one loop failing must not drop the rest
                ok_all = False
                lines.append(f"· **{name}** failed — {e}")
                continue
            if name == "import":
                me = res.get("me") or {}
                lines.append(f"· **import** {res.get('own', '?')} own + {res.get('niche', '?')} niche"
                             + (f" · @{me.get('username')} ({me.get('followers')} followers)" if me else ""))
            elif name == "study":
                lines.append(f"· **study** +{res.get('niche_new', 0)} niche · bank {res.get('bank', '?')}")
            elif name == "scan":
                lines.append(f"· **scan** {res.get('posts_scanned', 0)} posts · voice {res.get('voice', '?')}")
            else:
                lines.append(f"· **learn** refreshed {res.get('refreshed', 0)} posts")
        return lines, ok_all

    try:
        lines, ok_all = asyncio.run(_chain())
    except Exception as e:  # noqa: BLE001 — the chain itself (not one loop) died
        return f"study chain failed: {e}"
    tail = ("\n\n✅ Brain updated — everything I know about you is fresh." if ok_all
            else "\n\n⚠️ Study finished with errors — see the lines above.")
    return "\n".join(lines) + tail


def _cmd_digest(cfg: Config) -> str:
    from ..gen import digest as digest_mod
    d = digest_mod.build_digest(cfg)
    return digest_mod.render_text(d, str(db.get_setting("language") or "en"))


def approve_draft_tg(cfg: Config, draft_id: int) -> str:
    """Mirror of the dashboard approve: keep any proposed slot, else smart
    slot (or static cadence when off). Same gate — approving is the human
    action; publishing still only happens through the publish loop."""
    from ..gen import slots as slots_mod

    d = db.get_draft(draft_id)
    if not d or d["status"] not in ("draft", "approved"):
        return f"No approvable draft #{draft_id} — /drafts lists them."
    sched, reason = d.get("scheduled_at"), (d.get("meta") or {}).get("scheduled_reason")
    if sched is None:
        if cfg.agent.smart_slots:
            picked, reason = slots_mod.pick_slot_with_reason(
                cfg, d.get("kind") or "post", datetime.now())
            sched = picked.isoformat(timespec="seconds")
        else:
            sched = _next_static_slot(cfg)
    meta = d.get("meta") or {}
    if reason:
        meta["scheduled_reason"] = reason
    db.update_draft(draft_id, status="approved", scheduled_at=sched, meta_json=meta)
    db.log("telegram", f"draft {draft_id} approved from TG → {sched}")
    return (f"✅ Draft #{draft_id} approved — scheduled "
            f"{sched[:16].replace('T', ' ')}\n({reason})")


def _next_static_slot(cfg: Config) -> str:
    now = datetime.now()
    for offset in range(3):
        for t in (cfg.agent.post_times or ["09:00"]):
            hh, mm = map(int, str(t).split(":")[:2])
            slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if slot > now:
                return slot.isoformat(timespec="seconds")
    return (now.replace(second=0, microsecond=0)).isoformat(timespec="seconds")


def reject_draft_tg(draft_id: int) -> str:
    d = db.get_draft(draft_id)
    if not d or d["status"] not in ("draft", "approved"):
        return f"No draft #{draft_id} to reject — /drafts lists them."
    db.update_draft(draft_id, status="rejected")
    db.log("telegram", f"draft {draft_id} rejected from TG")
    return f"🗑 Draft #{draft_id} rejected."


def post_draft_tg(cfg: Config, text: str) -> str:
    """/post — the owner's own text, voice-checked, queued for approval.
    Never auto-publishes; source=tg marks the origin."""
    from ..gen.chat import draft_from_chat

    text = text.strip()
    if len(text) < 15:
        return "Give me a bit more text (15+ chars) — /post <your post>"
    did = draft_from_chat(cfg, text)  # voice meta attached, never rejects
    meta = db.get_draft(did).get("meta") or {}  # stamp the tg origin
    meta["source"], meta["via"] = "tg", "telegram"
    db.update_draft(did, meta_json=meta)
    db.log("telegram", f"/post draft {did} queued from TG (approval-gated)")
    return (f"📝 Saved as draft #{did} — queued for your approval like any "
            f"other draft. /drafts to review, /approve {did} to schedule it.")


# ---------------- update dispatch ----------------

def handle_update(cfg: Config, upd: dict) -> None:
    """One Telegram update → maybe one reply. Never raises."""
    try:
        _handle_update(cfg, upd)
    except Exception as e:  # noqa: BLE001 — one bad update must not kill polling
        db.log("telegram", f"update handler error: {e}", level="error")


def _handle_update(cfg: Config, upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = int((msg.get("chat") or {}).get("id") or 0)
    text = str(msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    db.log("telegram", f"inbound from chat {chat_id}: "
                       f"{text[:60]!r}", level="info")

    denied = _auth_reply(chat_id)
    if denied is not None:
        if denied:  # empty string = stay silent
            send_message(chat_id, denied)
        return

    cmd = parse_command(text)
    if cmd is None:
        send_stream(chat_id, chat_reply_tg_stream(cfg, chat_id, text))
        return
    name, args = cmd
    if name in ("start", "help"):
        reply: str = HELP_TEXT
    elif name == "status":
        reply = _cmd_status(cfg)
    elif name == "ideas":
        reply = _cmd_ideas()
    elif name == "drafts":
        reply = _cmd_drafts()
    elif name == "approve":
        reply = approve_draft_tg(cfg, _int_arg(args, "approve"))
    elif name == "reject":
        reply = reject_draft_tg(_int_arg(args, "reject"))
    elif name == "post":
        reply = post_draft_tg(cfg, args)
    elif name == "digest":
        reply = _cmd_digest(cfg)
    elif name == "study":
        reply = _cmd_study(cfg)
    else:
        reply = f"Unknown command /{name} — /help lists what I can do."
    send_message(chat_id, reply)


def _int_arg(args: str, cmd: str) -> int:
    try:
        return int(args.split()[0])
    except (ValueError, IndexError):
        return -1


# ---------------- poller lifecycle ----------------

MAX_CONCURRENT_HANDLERS = 4   # parallel update ceiling — 100 queued messages
                              # must not spawn 100 LLM calls
SHUTDOWN_GRACE_S = 10.0       # let in-flight replies land before "stopped"

_chat_tasks: dict[int, asyncio.Task] = {}  # chat id → its latest handler task


def _upd_chat_id(upd: dict) -> int:
    msg = upd.get("message") or upd.get("edited_message") or {}
    return int((msg.get("chat") or {}).get("id") or 0)


def status() -> dict:
    return {"state": _state["mode"], "enabled": is_enabled(),
            "chats": len(allowed_chats()), "offset": _state["offset"],
            "task_alive": bool(_state["task"] and not _state["task"].done())}


async def start(cfg: Config, force: bool = False) -> None:
    """Start long-polling when settings allow it. force=True is the test
    escape hatch (ignores env guard + enabled check; httpx still faked)."""
    if not force:
        if os.environ.get("OPENSTANLEY_NO_TELEGRAM") == "1":
            _state["mode"] = "disabled"
            return
        if not is_enabled():
            _state["mode"] = "disabled"
            db.log("system", "telegram poller not started (disabled or no token)")
            return
    if _state["task"] and not _state["task"].done():
        return
    _state["stop"].clear()
    _chat_tasks.clear()  # any survivors belong to the previous (dead) loop
    _state["task"] = asyncio.get_running_loop().create_task(_poll_loop(cfg))


async def stop() -> None:
    """Graceful shutdown: the in-flight poll finishes (≤25s), no new ones."""
    _state["stop"].set()
    task = _state["task"]
    _state["task"] = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if _state["mode"] != "bad_token":
        _state["mode"] = "disabled"


async def restart(cfg: Config, force: bool = False) -> None:
    await stop()
    await start(cfg, force=force)


async def _poll_loop(cfg: Config) -> None:
    """Long-poll loop. Updates are dispatched CONCURRENTLY (a slow LLM reply
    must not park every later message) with two guarantees: replies to the
    SAME chat stay in order (per-chat task chain), and concurrency is bounded
    (semaphore) so a burst can't spawn unbounded LLM calls. Different chats
    run in parallel."""
    token = bot_token()
    _state["mode"] = "polling"
    db.log("system", f"telegram poller started ({len(allowed_chats())} allowed chat(s))")
    sem = asyncio.Semaphore(MAX_CONCURRENT_HANDLERS)
    pending: set[asyncio.Task] = set()

    async def _dispatch(upd: dict, prev: asyncio.Task | None) -> None:
        cid = _upd_chat_id(upd)
        try:
            if prev is not None and not prev.done():
                # same chat → strict ordering. asyncio.wait (not gather/await)
                # never propagates prev's exception into this handler.
                await asyncio.wait({prev})
            async with sem:
                try:
                    await asyncio.to_thread(handle_update, cfg, upd)
                except Exception as e:  # noqa: BLE001 — handle_update's own contract
                    db.log("telegram", f"handler task error: {e}", level="error")
        finally:
            pending.discard(asyncio.current_task())
            if _chat_tasks.get(cid) is asyncio.current_task():
                del _chat_tasks[cid]

    try:
        while not _state["stop"].is_set():
            try:
                r = await asyncio.to_thread(_get_updates, token, _state["offset"])
            except Exception as e:  # noqa: BLE001 — network blip → backoff
                db.log("telegram", f"getUpdates failed: {_scrub(str(e), token)}",
                       level="warn")
                await asyncio.sleep(ERROR_SLEEP_S)
                continue
            if r.status_code in (401, 403, 404):
                _state["mode"] = "bad_token"
                db.log("telegram", "telegram rejected the bot token — "
                                   "poller stopped (update it in Settings)",
                       level="error")
                return
            if r.status_code != 200:
                await asyncio.sleep(ERROR_SLEEP_S)
                continue
            try:
                updates = r.json().get("result", [])
            except ValueError:
                updates = []
            for upd in updates:
                _state["offset"] = max(_state["offset"],
                                       int(upd.get("update_id", 0)) + 1)
                cid = _upd_chat_id(upd)
                t = asyncio.create_task(_dispatch(upd, _chat_tasks.get(cid)))
                _chat_tasks[cid] = t
                pending.add(t)
            if not updates:
                await asyncio.sleep(EMPTY_POLL_SLEEP_S)
    except asyncio.CancelledError:
        raise
    finally:
        # grace-wait in-flight handlers so replies land before "stopped";
        # anything still alive after the grace period is cancelled
        if pending:
            _done, alive = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_S)
            for t in alive:
                t.cancel()
        if _state["mode"] == "polling":
            _state["mode"] = "disabled"
        db.log("system", "telegram poller stopped")
