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
    "\nAnything else you type, I answer — same brain as the dashboard."
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


def _clip(text: str, limit: int = MSG_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


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
        r = _api(token, "sendMessage", {"chat_id": chat_id, "text": _clip(text)})
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
    lines = [f"🟡 {len(rows)} new draft(s) need your approval:"]
    lines += (f"· {draft_line(d, with_kind=True)}" for d in rows[:DRAFTS_PAGE])
    if len(rows) > DRAFTS_PAGE:
        lines.append(f"· +{len(rows) - DRAFTS_PAGE} more — /drafts")
    lines.append("Reply /approve <id> or /reject <id>, or open the dashboard.")
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
    sess = _sessions.setdefault(chat_id, [])
    sess.append({"role": role, "content": content})
    del sess[:-SESSION_CAP]  # cap the memory the TG chat keeps


def _history_turn(chat_id: int, user_message: str) -> str:
    """Mirror of chat._history_turn, but over this chat's private session."""
    hist = _sessions.get(chat_id, [])[:-1]
    if not hist:
        return user_message
    hist_text = "\n".join(f"{h['role'].upper()}: {h['content'][:400]}" for h in hist)
    return f"(conversation so far)\n{hist_text}\n\n(user) {user_message}"


def chat_reply_tg(cfg: Config, chat_id: int, user_message: str) -> str:
    """One OpenStanley answer on TG — the dashboard engine's pieces (system
    prompt + brain context, voice tuning, tool calls) over a per-chat
    session. Blocking (one LLM call) — callers run it off the event loop."""
    import dataclasses

    from ..gen import brain as brain_mod
    from ..gen import chat as chat_mod
    from ..gen import tools as tools_mod
    from ..gen.llm import LLMError

    _remember(chat_id, "user", user_message)
    llm_cfg = dataclasses.replace(cfg.llm, temperature=chat_mod._llm_temperature(),
                                  max_tokens=1200)
    try:
        reply = chat_mod.llm_chat(llm_cfg, system=chat_mod._system(cfg, user_message),
                                  user=_history_turn(chat_id, user_message))
    except LLMError as e:
        return f"(LLM error: {e})"

    tool_results = chat_mod._run_tools(cfg, reply)
    clean = tools_mod.strip_actions(reply)
    if tool_results:
        clean += "\n" + "\n".join(f"· {r['name']}: {'ok' if r.get('ok') else 'failed'}"
                                 for r in tool_results)
    _remember(chat_id, "assistant", clean)
    brain_mod.maybe_reflect_chat_async(cfg)  # every 10th message → reflect
    return clean


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
    head = f"#{d['id']}" + (f" [{d.get('kind') or 'post'}]" if with_kind else "")
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
    lines = [
        f"🪪 @{me.get('username', cfg.x.username or 'unknown')}"
        f" ({me.get('followers', '?')} followers, mode={cfg.x.mode})",
        f"🧭 autopilot {'ON' if ap.get('enabled') else 'off'}"
        + (f" — phase {ap.get('phase')}, next {ap.get('next_tick')}"
           if ap.get("enabled") else ""),
        f"🩺 health check: {smoke.get('status', 'never')}",
        f"💡 idea bank: {bank['count']} idea(s)"
        + (f", replenished {(bank.get('last') or {}).get('at', '?')[:10]}"
           if (bank.get("last") or {}).get("at") else ""),
        f"🛡 today: {caps.get('posts', 0)}/{cfg.x.max_posts_per_day} posts, "
        f"{caps.get('replies', 0)}/{cfg.x.max_replies_per_day} replies",
    ]
    return "\n".join(lines)


def _cmd_ideas() -> str:
    ideas = db.fresh_ideas(IDEAS_PAGE)
    if not ideas:
        return "Idea bank is empty — run the study loop (/status shows bank)."
    return "💡 Top ideas:\n" + "\n".join(
        f"{i}. {idea['title']} (score {idea['score']})"
        for i, idea in enumerate(ideas, 1))


def _cmd_drafts() -> str:
    drafts = db.drafts_by_status("draft", DRAFTS_PAGE)
    if not drafts:
        return "Nothing waiting — the approval queue is clear."
    return "⏳ Waiting for approval:\n" + "\n".join(
        draft_line(d, with_kind=True) for d in drafts
    ) + "\n/approve <id> · /reject <id>"


def _cmd_study(cfg: Config) -> str:
    """Full 'learn me' chain: import own+niche posts, study the niche, deep-scan
    voice, refresh metrics, reflect everything into the brain. Read-only on X.
    Reuses the server's loop runner — same code path as the dashboard buttons."""
    import asyncio

    async def _chain() -> list[str]:
        from ..server.__main__ import _run_loop
        out = []
        for name, label in (("import", "📥"), ("study", "📚"), ("scan", "🔬"), ("learn", "🧠")):
            r = await _run_loop(name)
            res = (r.body if hasattr(r, "body") else r).get("result", {}) if not isinstance(r, dict) else r.get("result", {})
            if name == "import":
                me = (res or {}).get("me") or {}
                out.append(f"📥 import: {res.get('own', '?')} own + {res.get('niche', '?')} niche"
                           + (f" · @{me.get('username')} ({me.get('followers')} followers)" if me else ""))
            elif name == "study":
                out.append(f"📚 study: +{(res or {}).get('niche_new', 0)} niche · bank {(res or {}).get('bank', '?')}")
            elif name == "scan":
                out.append(f"🔬 scan: {(res or {}).get('posts_scanned', 0)} posts · voice {(res or {}).get('voice', '?')}")
            else:
                out.append(f"🧠 learn: refreshed {(res or {}).get('refreshed', 0)} posts")
        return out

    try:
        lines = asyncio.run(_chain())
        return "\n".join(lines) + "\n\n✅ brain updated — everything I know about you is fresh."
    except Exception as e:  # noqa: BLE001
        return f"study chain failed: {e}"


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
    return f"✅ #{draft_id} approved — scheduled {sched[:16].replace('T', ' ')}\n({reason})"


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
    return f"🗑 #{draft_id} rejected."


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
        send_message(chat_id, chat_reply_tg(cfg, chat_id, text))
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
    token = bot_token()
    _state["mode"] = "polling"
    db.log("system", f"telegram poller started ({len(allowed_chats())} allowed chat(s))")
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
                await asyncio.to_thread(handle_update, cfg, upd)
            if not updates:
                await asyncio.sleep(EMPTY_POLL_SLEEP_S)
    except asyncio.CancelledError:
        raise
    finally:
        if _state["mode"] == "polling":
            _state["mode"] = "disabled"
        db.log("system", "telegram poller stopped")
