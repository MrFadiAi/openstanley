# OpenStanley v0.4.4 — Telegram Integration (reach your agent on TG)

Fadi's ask: "Add telegram integration so the ai agent can be also reached on telegram."
The agent already chats (gen.chat), drafts, reports (digest). Telegram becomes a SECOND
frontend: talk to OpenStanley from TG, approve drafts from TG, receive digests + approvals
in a chat. The dashboard stays primary; TG mirrors + notifies.

## 0. Bot setup (Fadi does once)
@BotFather → /newbot → token → set webhook later via the app's own connect flow.
No bot token exists yet in the repo — the integration must work WITHOUT a token
configured (disabled state) and activate when settings arrive.

## 1. `openstanley/integrations/telegram.py`
- Long-polling worker (async task on server start; `python-telegram-bot` is banned —
  use raw `httpx` against `https://api.telegram.org/bot<token>/getUpdates`, offset
  tracking, 25s timeout, graceful shutdown) — no webhook infra needed, local-first.
- Allowed chat IDs: settings `tg_allowed_chats` (comma list). Messages from other
  chats get one polite "not authorized" reply then silence. If empty → bot replies
  "set your chat id" with the seen chat id included (bootstrap UX).
- Command/message handling:
  - `/start`, `/help` — capabilities
  - `/status` — identity, autopilot, health, bank, caps today (reuse smoke/digest data)
  - `/ideas` — top 5 bank ideas with scores
  - `/drafts` — pending approvals (up to 5): id, kind, text preview, target chips,
    voice chip
  - `/approve <id>` — approve draft (schedules via smart slots); `/reject <id>`
  - `/post <text>` — voice-locked draft created directly from TG (goes to approval
    queue with source=tg, NOT auto-published; reply confirms queued)
  - plain text → `gen.chat` with brain context (same chat engine as the dashboard,
    session per chat id, capped history 20) — the agent ANSWERS on TG
  - `/digest` — today's digest text on demand
- Outbound: `notify(text)` helper — used by digest cron (parallel to webhook) and by
  the approval queue: when a new draft lands, TG gets a compact "needs approval"
  card. Rate-limit outbound: max 20 msgs/min, queue overflow drops with warn log.

## 2. Server wiring
- Settings: `tg_bot_token` (write-only, masked like webhook), `tg_allowed_chats`,
  `tg_enabled`. Changing token/enabled restarts the poller task cleanly.
- POST `/api/telegram/test` — sends "OpenStanley online" to first allowed chat.
- Settings UI section (EN+AR): token input (masked), chat ids, enable toggle, test
  button, status line (polling active / disabled / bad token).

## 3. Digest + approvals bridge
- Digest cron: after webhook POST, also `tg.notify(render_text(digest))` when enabled.
- New-draft hook (create/engage/mentions loops): enqueue TG approval card (non-blocking,
  failure never breaks the loop).

## 4. Tests (hermetic)
- Update parser (command/text/args); auth gate (disallowed chat silenced after one
  reply); offset tracking; chat via fake LLM (session memory, cap 20); /approve
  schedules through smart slots (spy); /post creates voice-locked draft with source=tg;
  notify rate-limit + drop; poller lifecycle (start/stop/restart on token change);
  digest bridge calls notify; draft hook enqueues card. ~14 tests → aim ~230.
  ALL network faked (httpx mocked at seam) — zero real TG calls in tests.

Hard rules: hermetic tests; ONE new module + minimal touches; approval gate applies
everywhere (TG can approve EXISTING drafts but nothing auto-publishes); token never
logged or returned by GET.
