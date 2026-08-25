"""Telegram frontend (v0.5.1) — reach OpenStanley from a chat app.

The dashboard stays primary; Telegram is a SECOND frontend that mirrors and
notifies: talk to OpenStanley (same chat engine, per-chat sessions), approve or
reject drafts, read status/ideas/digest, and receive the daily digest plus
"needs approval" cards the moment a loop drafts something.

v0.5.1 (FIX_BRIEF_TG_OUTPUT_POLISH): output parity with the web agent — the
markdown→HTML converter is total (no raw **, `, ##, tables or fences ever
reach the chat), progressive stream edits render only complete segments,
cards quote drafts verbatim in full (no previews, no `·` soup), and the
engage loop caps replies per author per batch.

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
import secrets
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import json

import httpx

from ..core import db
from ..core.config import Config, ROOT

API_URL = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_S = 25          # Telegram long-poll window
HTTP_TIMEOUT_S = 10.0        # sendMessage / getUpdates client timeout
EMPTY_POLL_SLEEP_S = 0.5     # breather between empty long-polls
ERROR_SLEEP_S = 5.0          # backoff after a failed poll
MAX_OUT_PER_MIN = 20         # outbound rate limit (drops beyond this)
SESSION_CAP = 20             # messages remembered per TG chat
DRAFTS_PAGE = 5              # /drafts previews
IDEAS_PAGE = 5               # /ideas rows
MSG_LIMIT = 4000             # Telegram hard limit is 4096 — clip under it
TOKEN_MIN_LEN = 10           # anything shorter is not a real bot token

FILE_URL = "https://api.telegram.org/file/bot{token}/{path}"
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # matches the /api/media upload cap

HELP_TEXT = (
    "I'm OpenStanley — your AI Head of Content, now on Telegram.\n\n"
    "/status — active account, autopilot, health, bank, today's caps\n"
    "/account — list accounts; /account <id> switches the active one.\n"
    "   ALL drafts follow the SELECTED account's voice + language.\n"
    "/ideas — top idea-bank angles\n"
    "/drafts — drafts waiting for your approval\n"
    "/approve <id> — approve a draft (it gets scheduled)\n"
    "/reject <id> — reject a draft\n"
    "(approval cards and /drafts carry one-tap buttons — tap, done)\n"
    "/img <id> — attach a photo to a draft (send the photo with this caption,\n"
    "           or just reply to a draft card with a photo)\n"
    "/thread <topic> — compose a 3-6 tweet thread draft\n"
    "/post <text> — save your own text as a draft for review\n"
    "or just send a VOICE NOTE — heard, transcribed, drafted from your words\n"
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

MEDIA_DIR = ROOT / "data" / "media"   # same folder /api/media writes to; monkeypatched in tests
_card_map: dict[int, dict[int, list[int]]] = {}  # chat → card message_id → previewed draft ids


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
                            "allowed_updates": ["message", "callback_query"]},
                      timeout=POLL_TIMEOUT_S + HTTP_TIMEOUT_S)


def _entities_rejected(r: httpx.Response) -> bool:
    """Telegram's 400 for HTML it can't parse ('can't parse entities')."""
    if getattr(r, "status_code", None) != 400:
        return False
    try:
        return "can't parse entities" in str(r.text).lower()
    except Exception:  # noqa: BLE001 — a probe must never raise
        return False


def _api_send_text(token: str, chat_id: int, text: str,
                   partial: bool = False,
                   reply_markup: str | None = None) -> httpx.Response:
    """sendMessage with HTML formatting; if Telegram rejects the entities,
    retried ONCE with parse_mode removed (plain, markers stripped) —
    formatting must never cost delivery."""
    params: dict = {"chat_id": chat_id, "text": _format_tg(text, partial=partial),
                    "parse_mode": "HTML"}
    if reply_markup:
        params["reply_markup"] = reply_markup
    r = _api(token, "sendMessage", params)
    if _entities_rejected(r):
        r = _api(token, "sendMessage",
                 {"chat_id": chat_id, "text": _clip(_md_to_plain(text))})
    return r


def _api_edit_text(token: str, chat_id: int, message_id: int, text: str,
                   partial: bool = False) -> httpx.Response:
    """editMessageText with the same formatting + plain-retry contract."""
    r = _api(token, "editMessageText",
             {"chat_id": chat_id, "message_id": message_id,
              "text": _format_tg(text, partial=partial), "parse_mode": "HTML"})
    if _entities_rejected(r):
        r = _api(token, "editMessageText",
                 {"chat_id": chat_id, "message_id": message_id,
                  "text": _clip(_md_to_plain(text))})
    return r


def _clip(text: str, limit: int = MSG_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------- markdown → Telegram HTML ----------------
#
# Hermes-style rich messages: bold labels, bullets, code, links — not walls of
# emoji. HTML (not MarkdownV2) because it needs no escape-everything traps.
# Contract: whatever the LLM emitted, the output must be something Telegram
# accepts — stray & < > are escaped BEFORE tags are inserted, and the
# conversion is TOTAL (v0.5.1, FIX_BRIEF_TG_OUTPUT_POLISH): unknown or
# unbalanced markdown is stripped, never shown raw. Web-only shapes degrade:
# `## Header` → bold line, tables → aligned <pre> block, nested lists → flat
# `•` bullets, `---` rules dropped, zero-width chars never emitted, and `·`
# (which renders as a box on mobile TG) never survives as a bullet/separator.

TG_MSG_LIMIT = 4096            # Telegram's hard ceiling for one message
BULLET = "•"                    # the house bullet — never the middle dot `·`

_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\n?(.*?)```", re.DOTALL)
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_ITALIC_RE = re.compile(r"\*(?!\s)([^*\n]+?)(?<!\s)\*")
_MD_STRIKE_RE = re.compile(r"~~(?!\s)([^~\n]+?)(?<!\s)~~")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_MD_HEADER_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_MD_HEADER_BARE_RE = re.compile(r"^#{1,6}[ \t]*$")
_MD_HR_RE = re.compile(r"^[ \t]*(?:-[ \t]*){3,}$|^[ \t]*(?:\*[ \t]*){3,}$"
                       r"|^[ \t]*(?:_[ \t]*){3,}$")
_MD_QUOTE_RE = re.compile(r"^&gt;[ \t]?")
_TAG_UNWRAP_RE = re.compile(r"</?(?:b|i|s|u|code)\b[^>]*>")
_MD_BULLET_RE = re.compile(r"^([ \t]*)(?:[-*+])[ \t]+")
_MD_DOT_BULLET_RE = re.compile(r"^[ \t]*·[ \t]?")
_MD_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_MD_TABLE_SEP_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$")
# zero-width chars render as boxes on mobile — stripped at the door
_ZW_RE = re.compile("[" + "".join(map(chr, (0x200b, 0x200c, 0x200d, 0x2060,
                                            0xfeff))) + "]")
_TAG_RE = re.compile(r"</?(?:b|i|u|s|code|pre|a|blockquote)\b[^>]*>")

_BLOCK_TAG = "\x00B{}\x00"     # placeholder tokens for stashed code (the
_CODE_TAG = "\x00C{}\x00"      # \x00 byte never occurs in real message text)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _bulletize(m: re.Match) -> str:
    """A markdown bullet → flat `• ` with (capped) indent for nesting depth."""
    depth = len(m.group(1).expandtabs(2))
    return ("  " * min(2, depth // 2)) + BULLET + " "


def _line_html(line: str) -> str:
    """One escaped line → TG line: headers bolded (hash dropped), HRs dropped,
    bullets flattened to `• ` with the old `·` bullets normalised too."""
    if _MD_HR_RE.match(line):
        return ""
    h = _MD_HEADER_RE.match(line)
    if h:  # the line is bold as a whole — inner markers/tags would only nest
        inner = _TAG_UNWRAP_RE.sub("", h.group(1).replace("**", "")).strip()
        return f"<b>{inner}</b>"
    if _MD_HEADER_BARE_RE.match(line):
        return ""
    line = _MD_BULLET_RE.sub(_bulletize, line)
    return _MD_DOT_BULLET_RE.sub(BULLET + " ", line)


def _table_pre(rows: list[str]) -> str:
    """GFM table rows → one aligned <pre> block (separator row dropped,
    columns padded, cells clipped at 40 so a wide table can't blow the 4096
    ceiling). Never raw pipes."""
    grid: list[list[str]] = []
    for ln in rows:
        if _MD_TABLE_SEP_RE.match(ln):
            continue
        cells = [c.strip()[:40] for c in ln.strip().strip("|").split("|")]
        grid.append(cells)
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    cols = [max((len(r[i]) if i < len(r) else 0) for r in grid)
            for i in range(width)]
    norm = ["  ".join((r[i] if i < len(r) else "").ljust(cols[i])
                      for i in range(width)) for r in grid]
    return "<pre>" + _esc("\n".join(norm)) + "</pre>"


def _md_to_tg_html(text: str) -> str:
    """Markdown-ish LLM output → Telegram HTML. Converts **bold**, *italic*,
    ~~strike~~, `code`, fenced ```blocks``` (→ <pre>, language tag dropped),
    [links](url), ## headers (→ bold line), tables (→ aligned <pre>), >
    quote blocks (→ <blockquote>, post candidates keep their own voice) and
    flattens -/*/+ bullets to `• `. Total: any leftover marker is stripped,
    so raw markdown symbols can never reach the chat."""
    text = _ZW_RE.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")

    blocks: list[str] = []      # code fences + rendered tables, restored last

    def _stash_block(html: str) -> str:
        blocks.append(html)
        return _BLOCK_TAG.format(len(blocks) - 1)

    text = _MD_CODE_BLOCK_RE.sub(
        lambda m: _stash_block(f"<pre>{_esc(m.group(1))}</pre>"), text)

    # contiguous table rows → one aligned <pre>, stashed like code so escaping
    # and the marker sweep can't touch it
    src = text.split("\n")
    kept: list[str] = []
    i = 0
    while i < len(src):
        if _MD_TABLE_ROW_RE.match(src[i]) or _MD_TABLE_SEP_RE.match(src[i]):
            tbl: list[str] = []
            while i < len(src) and (_MD_TABLE_ROW_RE.match(src[i])
                                    or _MD_TABLE_SEP_RE.match(src[i])):
                tbl.append(src[i])
                i += 1
            kept.append(_stash_block(_table_pre(tbl)))
            continue
        kept.append(src[i])
        i += 1
    text = "\n".join(kept)

    codes: list[str] = []

    def _stash_code(m: re.Match) -> str:
        codes.append(f"<code>{_esc(m.group(1))}</code>")
        return _CODE_TAG.format(len(codes) - 1)

    text = _MD_CODE_RE.sub(_stash_code, text)

    text = _esc(text)
    text = _MD_LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _MD_BOLD_RE.sub(r"<b>\1</b>", text)
    text = _MD_ITALIC_RE.sub(r"<i>\1</i>", text)
    text = _MD_STRIKE_RE.sub(r"<s>\1</s>", text)

    # quote lines → one <blockquote> per contiguous group; line shapes last
    src = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(src):
        q = _MD_QUOTE_RE.match(src[i])
        if not q:
            out.append(_line_html(src[i]))
            i += 1
            continue
        group: list[str] = []
        while i < len(src) and (m := _MD_QUOTE_RE.match(src[i])):
            group.append(_line_html(src[i][m.end():]))
            i += 1
        out.append("<blockquote>" + "\n".join(group) + "</blockquote>")
    text = "\n".join(out)

    # total-conversion sweep: unbalanced **/`/~~ markers are REMOVED, not
    # shown raw. Stashed code (and fences/tables) are restored AFTER, so
    # their literal contents keep their markers — that's what code means.
    text = text.replace("**", "").replace("`", "").replace("~~", "")
    for i, c in enumerate(codes):
        text = text.replace(_CODE_TAG.format(i), c)
    for i, b in enumerate(blocks):
        text = text.replace(_BLOCK_TAG.format(i), b)
    return text


def _md_to_plain(text: str) -> str:
    """Markdown → plain text (no tags, no markers) for the no-parse_mode
    retry path: even the plain fallback must never show raw markdown."""
    return _unescape(_TAG_RE.sub("", _md_to_tg_html(text)))


# ---------------- streaming partials ----------------
#
# Progressive edits render only COMPLETE markdown segments: the tail from the
# last unclosed marker is deferred to the next edit / the final edit, so a
# half-emitted **bold or ```fence never flashes as literal symbols.


def _last_unclosed(text: str) -> int | None:
    """Position where the incomplete segment starts, or None. Markers pair
    left-to-right (a closer clears its opener), so completed spans — even
    several of them — are never mistaken for unclosed tails."""
    fences = [m.start() for m in re.finditer(r"```", text)]
    if len(fences) % 2 == 1:      # unterminated code fence dominates
        return fences[-1]
    opens: dict[str, int] = {}    # marker → position of its unclosed opener
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if text[i:i + 2] == "**":
            if "**" in opens:
                del opens["**"]
            else:
                opens["**"] = i
            i += 2
            continue
        if ch == "`":
            if "`" in opens:
                del opens["`"]
            else:
                opens["`"] = i
            i += 1
            continue
        if ch == "[":
            j = text.find("]", i + 1)
            if j == -1:                     # bracket never closed
                opens["["] = i
                i += 1
                continue
            if j + 1 < n and text[j + 1] == "(" and text.find(")", j + 2) == -1:
                return i                    # [link](url — paren never arrived
            i = j + 1
            continue
        if (ch == "*" and i + 1 < n and text[i + 1] not in " *"):
            if "*" in opens:
                if text[i - 1] != " ":      # closer shape (italic)
                    del opens["*"]
            else:
                opens["*"] = i              # opener shape
            i += 1
            continue
        i += 1
    if opens:
        return max(opens.values())          # hide the least content first
    last_line = text.rsplit("\n", 1)[-1]
    if not text.endswith("\n") and (last_line.count("|") >= 2
                                    or last_line.lstrip().startswith("|")):
        return len(text) - len(last_line)   # table row still being typed
    return None


def _defer_incomplete(text: str) -> str:
    """Drop every trailing incomplete markdown segment (loop: cutting one can
    expose an earlier unclosed marker)."""
    while True:
        cut = _last_unclosed(text)
        if cut is None:
            return text
        text = text[:cut]


def _format_tg(text: str, partial: bool = False) -> str:
    """One outbound text, send-ready: clipped at the SOURCE (so a clip can
    never cut a tag in half), markdown → HTML, and a plain-text fallback when
    tag overhead would push the message past Telegram's 4096 ceiling.
    partial=True (progressive stream edits) renders only complete markdown
    segments — the incomplete tail waits for the next edit."""
    clipped = _clip(text)
    src = _defer_incomplete(clipped) if partial else clipped
    html = _md_to_tg_html(src)
    return html if len(html) <= TG_MSG_LIMIT else _clip(_md_to_plain(clipped))


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


def send_message(chat_id: int, text: str,
                 reply_markup: str | None = None) -> dict:
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
        r = _api_send_text(token, chat_id, text, reply_markup=reply_markup)
        ok = 200 <= r.status_code < 300
        if not ok:
            db.log("telegram", f"sendMessage to chat {chat_id} failed "
                               f"(HTTP {r.status_code})", level="warn")
        mid = None
        if ok:
            try:
                mid = int(r.json().get("result", {}).get("message_id"))
            except Exception:  # noqa: BLE001
                mid = None
        return {"ok": ok, "status_code": r.status_code, "message_id": mid,
                "error": None if ok else r.text[:200]}
    except Exception as e:  # noqa: BLE001 — sending must never take the caller down
        db.log("telegram", f"sendMessage to chat {chat_id} error: "
                           f"{_scrub(str(e), token)}", level="warn")
        return {"ok": False, "status_code": None,
                "error": _scrub(str(e), token)[:200]}


def send_photo(chat_id: int, image_name: str, caption: str = "") -> dict:
    """One outbound photo/document. Same contract as send_message: rate-
    limited, never raises. GIFs go as documents (TG won't render them as
    photos); anything else as sendPhoto. A failure here never costs the
    card — callers fall back to a text line."""
    token = bot_token()
    if not token:
        return {"ok": False, "status_code": None, "error": "no bot token"}
    path = MEDIA_DIR / image_name
    if not path.exists():
        return {"ok": False, "status_code": None, "error": "no such media file"}
    if not _rate_allow():
        db.log("telegram", f"rate limit hit — photo to chat {chat_id} dropped",
               level="warn")
        return {"ok": False, "status_code": None, "error": "rate limited"}
    method = "sendDocument" if image_name.lower().endswith(".gif") else "sendPhoto"
    field = "document" if method == "sendDocument" else "photo"
    try:
        r = httpx.post(API_URL.format(token=token, method=method),
                       files={field: (image_name, path.read_bytes())},
                       data={"chat_id": chat_id, "caption": _clip(caption)},
                       timeout=HTTP_TIMEOUT_S)
        ok = 200 <= r.status_code < 300
        if not ok:
            db.log("telegram", f"{method} to chat {chat_id} failed "
                               f"(HTTP {r.status_code})", level="warn")
        return {"ok": ok, "status_code": r.status_code,
                "error": None if ok else r.text[:200]}
    except Exception as e:  # noqa: BLE001
        db.log("telegram", f"{method} to chat {chat_id} error: "
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
                sent = _send_and_capture(token, chat_id, "".join(buf),
                                         partial=True)
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
                    _api_edit_text(token, chat_id, msg_id, "".join(buf),
                                   partial=True)
                    last_edit_t = now
                    chunks_since_edit = 0
                except Exception:  # noqa: BLE001 — a dropped edit is cosmetic
                    pass
        # final edit — always the complete text, fully converted
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
        # still try to deliver whatever we accumulated (partial: the text
        # genuinely ends mid-segment here — defer what never closed)
        try:
            if buf:
                if msg_id is None:
                    return _send_and_capture(token, chat_id, "".join(buf),
                                             partial=True)
                _api_edit_text(token, chat_id, msg_id, "".join(buf),
                               partial=True)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "status_code": None,
                "error": _scrub(str(e), token)[:200]}


def _stream_done(buf: list[str]) -> bool:
    """Heuristic: first sentence/line complete → worth showing the bubble."""
    text = "".join(buf)
    return ("\n" in text) or ("." in text[-3:])


def _send_and_capture(token: str, chat_id: int, text: str,
                      partial: bool = False) -> dict:
    r = _api_send_text(token, chat_id, text, partial=partial)
    ok = 200 <= r.status_code < 300
    mid = None
    if ok:
        try:
            mid = int(r.json().get("result", {}).get("message_id"))
        except Exception:  # noqa: BLE001
            mid = None
    return {"ok": ok, "status_code": r.status_code, "message_id": mid,
            "error": None if ok else r.text[:200]}


def _approve_keyboard(draft_ids: list[int]) -> str:
    """One [approve, reject, show] row per draft — one-tap decisions plus a
    read-only full-text view. callback_data stays tiny (a:/r:/s: + id) well
    under Telegram's 64 bytes."""
    rows = [[{"text": f"approve {i}", "callback_data": f"a:{i}"},
             {"text": f"reject {i}", "callback_data": f"r:{i}"},
             {"text": "show", "callback_data": f"s:{i}"}]
            for i in draft_ids[:DRAFTS_PAGE]]
    return json.dumps({"inline_keyboard": rows})


def notify(text: str, card_drafts: list[int] | None = None) -> dict:
    """Broadcast to every allowed chat (digest cron + approval cards).
    Rate-limited per message; failures are logged, never raised.
    card_drafts: when set, the sent card's message_id is recorded for
    reply-with-photo targeting."""
    chats = allowed_chats()
    if not is_enabled():
        return {"ok": False, "sent": 0, "chats": 0, "error": "telegram disabled"}
    sent = 0
    markup = _approve_keyboard(card_drafts) if card_drafts else None
    for c in chats:
        r = send_message(c, text, reply_markup=markup)
        if r["ok"]:
            sent += 1
            if card_drafts is not None and r.get("message_id"):
                _card_map.setdefault(c, {})[r["message_id"]] = list(card_drafts)
    return {"ok": sent > 0, "sent": sent, "chats": len(chats), "error": None}


def notify_bg(text: str) -> None:
    """Fire-and-forget notify from sync contexts (agent loops) — a daemon
    thread so a slow HTTP call can never block the caller."""
    threading.Thread(target=notify, args=(text,), daemon=True,
                     name="tg-notify").start()


def notify_new_drafts(draft_ids: list[int]) -> dict:
    """Compact 'needs approval' card for drafts a loop just created.
    Drafts with an image get their photo pushed right after the text card,
    so the human SEES the visual before /approve."""
    if not draft_ids:
        return {"ok": False, "sent": 0, "chats": 0, "error": "no drafts"}
    rows = [d for d in (db.get_draft(i) for i in draft_ids) if d]
    if not rows:
        return {"ok": False, "sent": 0, "chats": 0, "error": "no drafts"}
    previewed = [d["id"] for d in rows[:DRAFTS_PAGE]]
    result = notify(drafts_card(rows), card_drafts=previewed)
    for d in rows[:DRAFTS_PAGE]:
        if not d.get("image"):
            continue
        caption = (f"draft #{d['id']} — {(d.get('text') or '')[:180]}\n"
                   f"reply /approve {d['id']} or /reject {d['id']}")
        for chat in allowed_chats():
            if send_photo(chat, d["image"], caption)["ok"]:
                continue
            send_message(chat, f"draft #{d['id']} (image attached — view it in Inbox)")
    return result


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
                f"{BULLET} {r['name']}: {'ok' if r.get('ok') else 'failed'}"
                for r in tool_results)

    # post candidates (markdown quote blocks) → real drafts, exactly what the
    # dashboard's approval cards hold. Saving is safe; publishing is not ours.
    # Two guards (user report 22:48): quotes that repeat an EXISTING draft
    # ("show me the post" re-quotes it) are skipped, and a run of SHORT
    # quote lines is a THREAD — saved as one threaded draft, not fragments.
    draft_ids: list[int] = []
    cands = chat_mod._extract_candidates(clean, cfg)
    with db.connect() as _c:
        recent = {r["text"].strip() for r in _c.execute(
            "SELECT text FROM drafts WHERE status IN ('draft','approved') "
            "ORDER BY id DESC LIMIT 60")}
    cands = [c for c in cands if c["text"].strip() not in recent]
    if len(cands) >= 3 and all(len(c["text"]) < 110 for c in cands):
        texts = [c["text"] for c in cands]
        try:
            did = db.add_draft(text=texts[0], thread=texts, kind="post",
                               temperature="chat",
                               meta={"source": "chat", "via": "tg-thread-merge",
                                     "language": cands[0].get("language")})
            draft_ids.append(did)
            db.log("chat", f"thread candidate merged: {len(texts)} lines → #{did}")
            cands = []
        except Exception as e:  # noqa: BLE001
            db.log("telegram", f"thread merge failed: {e}", level="warn")
    for cand in cands:
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

# ---------------- draft cards (v0.5.1 redesign) ----------------
#
# Structure over symbols (FIX_BRIEF_TG_OUTPUT_POLISH): full draft text
# verbatim (drafts are ≤280 chars — they fit, nothing is cut), one block per
# draft separated by a blank line, em-dash chips, no `·` soup, emoji only as
# the leading section marker.

QUOTE_HARD_CAP = 800            # safety ceiling for pathological /post text


def _draft_head(d: dict, show_target: bool = True) -> str:
    """'#2321 — reply to @naval — voice 100%': em-dash chips, never a dot.
    show_target=False when the card's context line already names the author."""
    meta = d.get("meta") or {}
    parts = [f"#{d['id']}"]
    if show_target and d.get("kind") == "reply":
        who = meta.get("target_author") or meta.get("author")
        if who:
            parts.append(f"reply to @{who}")
    v = meta.get("voice") or {}
    score = v.get("score") if v.get("score") is not None else meta.get("voice_match")
    if score is not None:
        parts.append(f"voice {score}%")
    return " — ".join(parts)


def _quote(text: str) -> str:
    """Draft text quoted verbatim, whitespace collapsed to one line. The `…`
    appears ONLY at a true truncation point (the safety cap, never drafts)."""
    text = " ".join((text or "").split())
    if len(text) > QUOTE_HARD_CAP:
        text = text[: QUOTE_HARD_CAP - 1].rsplit(" ", 1)[0] + "…"
    return f"“{text}”"


def drafts_card(drafts: list[dict]) -> str:
    """The approval card — ⏳ header, one context line, one block per draft
    (head line + verbatim quote), /approve footer."""
    drafts = [d for d in drafts if d]
    if not drafts:
        return "Nothing waiting — the approval queue is clear."
    n = len(drafts)
    lines = [f"⏳ {n} draft{'s' if n != 1 else ''} waiting for approval"]
    authors = []
    for d in drafts:
        meta = d.get("meta") or {}
        if d.get("kind") == "reply":
            who = meta.get("target_author") or meta.get("author")
            if who:
                authors.append(who)
    one_author = len(authors) == n and len(set(authors)) == 1 if authors else False
    if one_author:
        lines.append(f"Replies drafted to @{authors[0]}'s recent posts:")
    elif len(authors) == n:
        lines.append("Replies drafted to recent posts:")
    else:
        lines.append("Waiting for your review:")
    lines.append("")
    for d in drafts[:DRAFTS_PAGE]:
        lines.append(_draft_head(d, show_target=not one_author))
        lines.append(_quote(d.get("text") or ""))
        lines.append("")
    if n > DRAFTS_PAGE:
        lines.append(f"{n - DRAFTS_PAGE} more in the queue — /drafts")
        lines.append("")
    lines.append("Reply /approve <id> or /reject <id>")
    return "\n".join(lines).rstrip()


def _cmd_status(cfg: Config) -> str:
    from ..core.safety import usage
    from ..gen import autopilot as ap_mod
    from ..gen import ideas as ideas_mod

    me = db.get_me()
    account = db.get_account(db.active_account()) or {}
    handle = me.get("username") or account.get("handle") or cfg.x.username or "unknown"
    ap = ap_mod.get_state()
    smoke = db.get_setting("smoke_last") or {}
    bank = ideas_mod.bank_health()
    caps = usage()
    ap_line = (f"{BULLET} **Autopilot** on — phase {ap.get('phase')}, "
               f"next {ap.get('next_tick')}" if ap.get("enabled")
               else f"{BULLET} **Autopilot** off")
    bank_line = f"{BULLET} **Idea bank** {bank['count']} idea(s)"
    if (bank.get("last") or {}).get("at"):
        bank_line += f", replenished {bank['last']['at'][:10]}"
    lines = [
        f"🤖 Account #{db.active_account()} — @{handle}"
        f" ({me.get('followers', '?')} followers, mode {cfg.x.mode})",
        "",
        ap_line,
        f"{BULLET} **Health check** {smoke.get('status', 'never')}",
        bank_line,
        f"{BULLET} **Today** {caps.get('posts', 0)}/{cfg.x.max_posts_per_day} posts, "
        f"{caps.get('replies', 0)}/{cfg.x.max_replies_per_day} replies",
    ]
    return "\n".join(lines)


def _cmd_account(args: str) -> str:
    """v0.5.0 — list accounts, or switch the active one (/account 2)."""
    parts = args.split()
    accounts = db.list_accounts()
    if not parts:
        lines = [f"👤 Accounts ({len(accounts)})"]
        for a in accounts:
            mark = "✅" if a["active"] else BULLET
            fol = f", {a['followers']} followers" if a["followers"] is not None else ""
            posts = f", {a['own_posts']} posts" if a["own_posts"] else ""
            lines.append(f"{mark} #{a['id']}. @{a['handle'] or 'no handle yet'}{fol}{posts}")
        lines.append("")
        lines.append(f"Active: #{db.active_account()} — /account <id> switches.")
        return "\n".join(lines)
    try:
        target = int(parts[0])
    except ValueError:
        return "Usage: /account [id]"
    if not db.set_active_account(target):
        return f"No account #{target} — /account lists them."
    try:  # rebuild the agent so cookie mode uses the new account's cookies
        from ..server.__main__ import _rebuild_agent
        _rebuild_agent()
    except Exception as e:  # noqa: BLE001 — the switch itself already happened
        db.log("telegram", f"account switch agent rebuild failed: {e}", level="warn")
    account = db.get_account(target) or {}
    db.log("accounts", f"TG switched active account → #{target}")
    return (f"✅ Active account → #{target} "
            f"(@{account.get('handle') or 'no handle yet'}) — everything now "
            f"reads and writes this account. /status to confirm.")


def _cmd_ideas() -> str:
    ideas = db.fresh_ideas(IDEAS_PAGE)
    if not ideas:
        return "Idea bank is empty — /study refills it."
    return ("💡 Idea bank — top angles\n\n" + "\n".join(
        f"{BULLET} {idea['title']} (score {idea['score']})" for idea in ideas))


def _latest_draft_id() -> int:
    with db.connect() as c:
        (m,) = c.execute("SELECT MAX(id) FROM drafts").fetchone() or (0,)
        return int(m or 0)


def _push_mini_card(chat_id: int, before_id: int) -> None:
    """Anything that created a draft during this TG interaction gets a
    one-tap approve/reject card right after — chat candidates, /thread,
    /post, tool saves. Tapping behaves exactly like the loop cards
    (live rewrite, slot shown)."""
    try:
        with db.connect() as c:
            rows = c.execute(
                "SELECT id, kind, text, meta_json FROM drafts "
                "WHERE id > ? AND status = 'draft' ORDER BY id LIMIT 5",
                (before_id,)).fetchall()
    except Exception:  # noqa: BLE001
        return
    if not rows:
        return
    ids = [r["id"] for r in rows]
    lines = ["saved, your call:"]
    # FULL text, same quote contract as the loop cards — the human approves
    # what they actually read, never a truncated 80-char preview
    import json as _json
    for r in rows:
        try:
            meta = _json.loads(r["meta_json"] or "{}")
        except Exception:  # noqa: BLE001
            meta = {}
        lines.append(_draft_head({"id": r["id"], "kind": r["kind"],
                                  "meta": meta}, show_target=False))
        lines.append(_quote(r["text"] or ""))
        lines.append("")
    r = send_message(chat_id, chr(10).join(lines),
                     reply_markup=_approve_keyboard(ids))
    if r.get("message_id"):
        _card_map.setdefault(chat_id, {})[r["message_id"]] = ids


def _cmd_thread(cfg: Config, args: str) -> str:
    """/thread <topic> — compose a 3-6 tweet thread draft (approval-gated)."""
    import httpx as _hx
    topic = args.strip()
    if not topic:
        return "Usage: /thread <topic> — e.g. /thread lessons from my first month on X"
    try:
        r = _hx.post("http://127.0.0.1:7878/api/threads",
                     json={"topic": topic}, timeout=120)
        body = r.json()
        if r.status_code == 200:
            return (f"Thread drafted #{body['draft_id']} — {body['tweets']} tweets"
                    " on your keyboard waiting: /drafts (or the card above)")
        return f"Thread failed: {body.get('detail', r.status_code)}"
    except Exception as e:  # noqa: BLE001
        return f"Thread failed: {e}"


def _cmd_drafts() -> str:
    return drafts_card(db.drafts_by_status("draft", DRAFTS_PAGE))


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
                lines.append(f"{BULLET} **{name}** timed out after {STUDY_LOOP_TIMEOUT_S}s")
                continue
            except Exception as e:  # noqa: BLE001 — one loop failing must not drop the rest
                ok_all = False
                lines.append(f"{BULLET} **{name}** failed — {e}")
                continue
            if name == "import":
                me = res.get("me") or {}
                lines.append(f"{BULLET} **import** {res.get('own', '?')} own + {res.get('niche', '?')} niche posts"
                             + (f", @{me.get('username')} ({me.get('followers')} followers)" if me else ""))
            elif name == "study":
                lines.append(f"{BULLET} **study** +{res.get('niche_new', 0)} niche, bank {res.get('bank', '?')}")
            elif name == "scan":
                lines.append(f"{BULLET} **scan** {res.get('posts_scanned', 0)} posts, voice {res.get('voice', '?')}")
            else:
                lines.append(f"{BULLET} **learn** refreshed {res.get('refreshed', 0)} posts")
        return lines, ok_all

    try:
        lines, ok_all = asyncio.run(_chain())
    except Exception as e:  # noqa: BLE001 — the chain itself (not one loop) died
        return f"study chain failed: {e}"
    tail = ("\n\n✅ Brain updated — everything I know about you is fresh." if ok_all
            else "\n\n⚠️ Study finished with errors — see the lines above.")
    return "🔍 Study report\n\n" + "\n".join(lines) + tail


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
    """Next cadence slot that isn't already holding an approved draft —
    mass one-tap approvals must spread, not stack on one 09:00."""
    from ..gen.slots import nudge_free, taken_slots
    now = datetime.now()
    for _offset in range(3):
        for t in (cfg.agent.post_times or ["09:00"]):
            hh, mm = map(int, str(t).split(":")[:2])
            slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if slot > now:
                at, _why = nudge_free(slot, cfg, taken_slots())
                return at.isoformat(timespec="seconds")
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

def _download_tg_photo(token: str, file_id: str) -> bytes:
    """file_id → bytes. Two calls: getFile → GET file. Raises on any
    failure — the caller turns it into a human message."""
    r = _api(token, "getFile", {"file_id": file_id})
    if not (200 <= r.status_code < 300):
        raise RuntimeError(f"getFile HTTP {r.status_code}")
    fp = (r.json().get("result") or {}).get("file_path")
    if not fp:
        raise RuntimeError("no file_path in getFile response")
    fr = httpx.get(FILE_URL.format(token=token, path=fp), timeout=HTTP_TIMEOUT_S)
    if not (200 <= fr.status_code < 300):
        raise RuntimeError(f"download HTTP {fr.status_code}")
    return fr.content


def _save_tg_photo(token: str, msg: dict) -> str:
    """Download the largest photo size and store it in MEDIA_DIR with the
    standard media_<ts>_<hex> name. Returns the stored name."""
    sizes = msg.get("photo") or []
    if not sizes:  # _handle_photo only calls us when photo is truthy; belt+braces
        raise ValueError("no photo sizes in message")
    biggest = max(sizes, key=lambda s: s.get("file_size") or 0)
    if (biggest.get("file_size") or 0) > MAX_IMAGE_BYTES:
        raise ValueError(f"photo too large (max {MAX_IMAGE_BYTES // (1024*1024)}MB)")
    data = _download_tg_photo(token, biggest["file_id"])
    name = f"media_{int(time.time())}_{secrets.token_hex(3)}.jpg"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(data)
    return name


def _attach_photo(cfg: Config, chat_id: int, msg: dict, target: int | None,
                  hint: str) -> None:
    """Shared tail: attach the photo in `msg` to draft `target` (or reply
    with `hint` when no target), and say what happened."""
    token = bot_token()
    if target is None:
        send_message(chat_id, hint)
        return
    d = db.get_draft(target)
    if not d or d.get("status") not in ("draft", "approved"):
        send_message(chat_id, f"Draft #{target} isn't waiting — /drafts lists them.")
        return
    try:
        name = _save_tg_photo(token, msg)
    except ValueError as e:
        send_message(chat_id, str(e))
        return
    except Exception as e:  # noqa: BLE001
        db.log("telegram", f"photo download failed: {_scrub(str(e), token)}",
               level="warn")
        send_message(chat_id, "Couldn't fetch the photo from Telegram — try again.")
        return
    db.update_draft(target, image=name)
    db.log("telegram", f"photo attached to draft #{target} ({name})")
    send_message(chat_id, f"Attached to draft #{target} ✓")


def _handle_photo(cfg: Config, chat_id: int, msg: dict) -> None:
    """Photo arrived. Target draft: /img <id> caption > reply-to-card."""
    cap = str(msg.get("caption") or "").strip()
    cmd = parse_command(cap) if cap.startswith("/") else None
    hint = ("Which draft? Send the photo again with a caption like "
            "`/img 12` — /drafts lists the ids.")
    if cmd and cmd[0] == "img":
        target = _int_arg(cmd[1], "img")
        if target < 0:
            send_message(chat_id, "Use `/img <id>` — e.g. `/img 12`.")
            return
    else:
        replied = (msg.get("reply_to_message") or {}).get("message_id")
        ids = (_card_map.get(chat_id) or {}).get(replied) if replied else None
        if ids is None:
            _attach_photo(cfg, chat_id, msg, None, hint)
            return
        if len(ids) != 1:
            send_message(chat_id, "That card lists several drafts — send the "
                                  "photo with a caption like `/img 12`.")
            return
        target = ids[0]
    _attach_photo(cfg, chat_id, msg, target, hint)


def handle_update(cfg: Config, upd: dict) -> None:
    """One Telegram update → maybe one reply. Never raises."""
    try:
        _handle_update(cfg, upd)
    except Exception as e:  # noqa: BLE001 — one bad update must not kill polling
        db.log("telegram", f"update handler error: {e}", level="error")


def _handle_voice(cfg: Config, chat_id: int, msg: dict) -> None:
    """Voice note -> text -> the normal chat brain. Talk to OpenStanley
    like a person: ramble about a post idea, it drafts from your words."""
    from ..gen import voice_notes as vn
    token = bot_token()
    v = msg.get("voice") or {}
    file_id = v.get("file_id")
    if not token or not file_id:
        send_message(chat_id, "Voice note came without a file — try again.")
        return
    try:
        r = _api(token, "getFile", {"file_id": file_id})
        fp = (r.json().get("result") or {}).get("file_path")
        if not fp:
            raise RuntimeError("no file_path")
        fr = httpx.get(FILE_URL.format(token=token, path=fp),
                       timeout=HTTP_TIMEOUT_S)
        if fr.status_code != 200:
            raise RuntimeError(f"download HTTP {fr.status_code}")
        text = vn.transcribe(fr.content)
    except Exception as e:  # noqa: BLE001
        db.log("telegram", f"voice download failed: {_scrub(str(e), token)}",
               level="warn")
        send_message(chat_id, "Couldn't fetch the voice note — try again.")
        return
    if not text:
        send_message(chat_id, "I couldn't hear anything in that voice note.")
        return
    send_message(chat_id, f"heard: {text[:300]}")
    send_stream(chat_id, chat_reply_tg_stream(cfg, chat_id, text))


def _fmt_slot(iso: str) -> str:
    """'2026-08-21T09:00:00' → 'Fri 09:00' — the card shows WHEN it ships."""
    try:
        return datetime.fromisoformat(iso).strftime("%a %H:%M")
    except ValueError:
        return iso[:16].replace("T", " ")


def _card_status_line(d: dict) -> str:
    """One line per draft on the live approval card."""
    if d["status"] == "rejected":
        return f"❌ #{d['id']} — rejected"
    if d["status"] == "published":
        return f"✅ #{d['id']} — live on X"
    if d["status"] == "approved" and d.get("scheduled_at"):
        return f"✅ #{d['id']} — scheduled {_fmt_slot(d['scheduled_at'])}"
    return f"⏳ #{d['id']} — {(d.get('text') or '').strip()[:60]}"


def _rebuild_card(chat_id: int, message_id: int) -> None:
    """The approval card is LIVE: every decision rewrites this message —
    decided drafts show their outcome (and their slot), pending drafts keep
    their one-tap buttons. Nobody disappears; everything stays visible."""
    token = bot_token()
    ids = (_card_map.get(chat_id) or {}).get(message_id)
    if not token or not message_id:
        return
    if not ids:
        # unknown message (e.g. an old card from before this feature) —
        # just drop its buttons so no stale tap fires twice
        _api(token, "editMessageReplyMarkup",
             {"chat_id": chat_id, "message_id": message_id})
        return
    rows = [r for r in (db.get_draft(i) for i in ids) if r]
    pending = [r["id"] for r in rows if r["status"] == "draft"]
    lines = ["⏳ Approvals — tap a button; this card tracks the rest:"]
    lines.extend(_card_status_line(r) for r in rows)
    lines.append("")
    lines.append("tap show on the card for any full draft")
    _api_edit_text(token, chat_id, message_id, chr(10).join(lines))
    markup = {"reply_markup": _approve_keyboard(pending)} if pending else {}
    _api(token, "editMessageReplyMarkup",
         {"chat_id": chat_id, "message_id": message_id, **markup})


def _handle_callback(cfg: Config, cb: dict) -> None:
    """Inline-button tap (approve/reject). One tap = one decision: the
    callback gets answered (spinner stops, result as toast) and the card's
    buttons are cleared so a second tap can't double-fire."""
    token = bot_token()
    if not token:
        return
    msg = cb.get("message") or {}
    chat_id = int((msg.get("chat") or {}).get("id") or 0)
    if not chat_id:
        return
    if chat_id not in allowed_chats():
        _api(token, "answerCallbackQuery",
             {"callback_query_id": cb.get("id"), "text": "not authorized"})
        return
    action, _, sid = str(cb.get("data") or "").partition(":")
    try:
        draft_id = int(sid)
    except ValueError:
        draft_id = -1
    if action == "s":
        # read-only: the full draft as its own message, card stays live
        db.log("telegram", f"show tap for draft #{draft_id}")
        d = db.get_draft(draft_id)
        if d and d["status"] in ("draft", "approved"):
            full = (f"#{draft_id} [{d.get('kind') or 'post'}] FULL DRAFT:" + chr(10)
                    + _quote(d["text"] or ""))
            if d.get("thread"):
                full += chr(10) + chr(10).join(
                    f"{n+1}. {_quote(t)}" for n, t in enumerate(d["thread"][1:]))
            link = (d.get("meta") or {}).get("link_reply")
            if link:
                full += chr(10) + f"link in first reply: {link}"
            send_message(chat_id, full)
            reply = ""  # silent toast; the full text IS the response
        else:
            reply = f"No draft #{draft_id} waiting — /drafts lists them."
        _api(token, "answerCallbackQuery",
             {"callback_query_id": cb.get("id"), "text": reply[:190]})
        return  # show never rewrites or clears the card
    if action == "a":
        reply = approve_draft_tg(cfg, draft_id)
    elif action == "r":
        reply = reject_draft_tg(draft_id)
    else:
        reply = "unknown button"
    _api(token, "answerCallbackQuery",
         {"callback_query_id": cb.get("id"), "text": reply[:190]})
    _rebuild_card(chat_id, msg.get("message_id"))


def _handle_update(cfg: Config, upd: dict) -> None:
    cb = upd.get("callback_query")
    if cb:
        _handle_callback(cfg, cb)
        return
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = int((msg.get("chat") or {}).get("id") or 0)
    text = str(msg.get("text") or "").strip()
    photo = msg.get("photo")
    has_media = bool(photo or msg.get("document") or msg.get("voice"))
    if not chat_id or (not text and not has_media):
        return
    db.log("telegram", f"inbound from chat {chat_id}: "
                       f"{(text or 'photo')[:60]!r}", level="info")

    denied = _auth_reply(chat_id)
    if denied is not None:
        if denied:  # empty string = stay silent
            send_message(chat_id, denied)
        return

    if not text and has_media:
        if photo:
            _handle_photo(cfg, chat_id, msg)
        elif msg.get("voice"):
            _handle_voice(cfg, chat_id, msg)
        else:
            send_message(chat_id, "Photos and voice notes, yes — videos aren't supported yet.")
        return

    cmd = parse_command(text)
    if cmd is None:
        before = _latest_draft_id()
        send_stream(chat_id, chat_reply_tg_stream(cfg, chat_id, text))
        _push_mini_card(chat_id, before)
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
        ids = [d["id"] for d in db.drafts_by_status("draft", DRAFTS_PAGE)]
        r = send_message(chat_id, reply,
                         reply_markup=_approve_keyboard(ids) or None)
        if r.get("message_id"):
            _card_map.setdefault(chat_id, {})[r["message_id"]] = ids
        return
    elif name == "approve":
        reply = approve_draft_tg(cfg, _int_arg(args, "approve"))
    elif name == "reject":
        reply = reject_draft_tg(_int_arg(args, "reject"))
    elif name == "post":
        before = _latest_draft_id()
        reply = post_draft_tg(cfg, args)
        send_message(chat_id, reply)
        _push_mini_card(chat_id, before)
        return
    elif name == "train":
        send_message(chat_id, "Deep training the brain on the active "
                              "account. This takes a few minutes, "
                              "I'll report when done.")
        import httpx as _hx
        try:
            r = _hx.post("http://127.0.0.1:7878/api/deep-train",
                         timeout=900)
            body = r.json().get("report", {}) if r.status_code == 200 else {}
            if body:
                report = (f"Deep train done for @{body.get('handle')} "
                          f"({body.get('seconds')}s):" + chr(10)
                          + f"posts {body.get('posts_ingested')} + replies "
                            f"{body.get('replies_ingested')} ingested" + chr(10)
                          + f"brain: {body.get('brain_rules')} rules, "
                            f"{body.get('journal_entries')} journal entries, "
                            f"{body.get('hooks')} hooks")
            else:
                report = f"Deep train failed: {r.text[:150]}"
        except Exception as e:  # noqa: BLE001
            report = f"Deep train failed: {e}"
        send_message(chat_id, report)
        return
    elif name == "thread":
        before = _latest_draft_id()
        reply = _cmd_thread(cfg, args)
        send_message(chat_id, reply)
        _push_mini_card(chat_id, before)
        return
    elif name == "digest":
        reply = _cmd_digest(cfg)
    elif name == "study":
        reply = _cmd_study(cfg)
    elif name == "account":
        reply = _cmd_account(args)
    else:
        reply = f"Unknown command /{name} — /help lists what I can do."
    send_message(chat_id, reply)


def _int_arg(args: str, cmd: str) -> int:
    try:
        return int(args.split()[0].lstrip("#"))  # '#2379' reads like '2379'
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
