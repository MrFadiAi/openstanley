# OpenStanley v0.4.2 — Daily Digest (the agent reports to its owner)

The agent now does a lot autonomously (autopilot phases, scans, metrics, voice lock,
smart slots, mentions) — but the human only sees it by opening the dashboard. OpenStanley's
relationship model is "your employee": it should proactively REPORT what it did, what it
learned, and what needs your decision. Fadi checks Telegram — the dashboard is second.

Generate a daily digest and surface it everywhere:

## 1. `openstanley/gen/digest.py`
- `build_digest(cfg, day) -> Digest` gathering from the DB (all local, no X reads):
  - **did**: loops run (from agent_log per loop name), posts published (count + top by
    engagement), replies sent, drafts rejected by voice lock (count + top violation),
    engage targets rejected (count + top reason), mentions handled/pending
  - **learned**: brain journal entries for the day (top 3, one-line summaries),
    rules added/changed, strategy deltas
  - **needs you**: pending approvals (count + first 2 previews), autopilot state,
    cookie/smoke health (from smoke_last: red/amber/green + age)
  - **numbers**: followers delta (identity snapshots), avg engagement rate,
    best post of day
  - **tomorrow**: top smart slots with reasons, ideas remaining in bank
- `render_markdown(digest)` and `render_text(digest)` (compact, emoji-headed lines,
  EN; AR variant via existing lang module pattern).

## 2. Delivery — webhook (Telegram-ready)
- `POST /api/digest/send` + scheduler job daily at `[agent] digest_hour` (default 20:00):
  - If a webhook URL is configured in settings (`digest_webhook_url`), POST the text
    digest as `{text: ...}` (generic JSON — works with Telegram bot sendMessage via
    relay, Discord, Slack-compatible).
  - Always store last digest in settings `digest_last` + write `data/digests/YYYY-MM-DD.md`.
- Settings UI section: webhook URL input (masked), digest hour, "Send test digest" button,
  "Preview today" button (opens rendered modal). EN+AR.

## 3. Dashboard digest tab
- New "Digest" panel on Insights (or its own tab if nav space allows — your call,
  keep it clean): today's digest rendered (markdown-lite), history picker (last 7 days
  from data/digests/), send button. RTL-safe, i18n.

## 4. Tests (hermetic)
- Digest assembly from fixture DB (all sections populated); empty-day digest (zeros +
  honest "nothing published" lines, no crash); markdown/text render shape; webhook
  send (mock HTTP — assert URL + payload; no real network); scheduler job registers at
  configured hour; file written to data/digests; API endpoints; AR render exists.
  ~11 tests → aim ~205. No network, suite stays green.

Hard rules: hermetic; digest reads DB/settings ONLY (no X calls, no LLM needed —
journal summaries come from stored reflection outputs); ONE new module + touches.
