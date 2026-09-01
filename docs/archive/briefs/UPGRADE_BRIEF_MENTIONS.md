# OpenStanley v0.3.9 — Mention Inbox (never miss a reply to the agent)

The account is live and posting — which means people will reply/mention @orbexai.
Right now there is NO path from an incoming mention to a response. The agent posts
into the void and never converses. OpenStanley's engage loop only targets SEARCH results,
not direct conversations. For an "AI agent persona" account, replying to people who
talk to it IS the product.

Build the mention inbox:

## 1. `openstanley/gen/mentions.py`
- `fetch_mentions(x, limit=30)` — `x.mentions()` (exists, wrapped) → normalize to
  `{x_id, author, text, created_at, tweet_link, conversation_id, reply_to_me}`.
  Skip own tweets; dedupe by x_id against DB (`posts` + a new `mentions` table
  `seen_mentions(x_id PK, author, text, created_at, first_seen, handled INTEGER DEFAULT 0)`).
- `pending_mentions()` — seen but `handled=0`, newest first, cap 50.
- `draft_mention_reply(cfg, mention)` — LLM drafts a reply using brain context +
  conversation context (fetch parent tweet text if reply_to_me) with the SAME voice
  rubric as engage; meta `{source: "mention", target_author, target_score: null}`
  (mention replies bypass the engage gate — someone talked to us directly).
  Approval gate applies as usual (never auto-publish unless autopilot auto-approve on).

## 2. Loop + autopilot wiring
- New loop name `mentions` added to LOOP_NAMES + agent method `Agent.mentions()`:
  fetch → store → draft replies for up to N (config `[agent] mention_drafts_per_run`, default 3)
  → mark handled once a draft exists for that mention.
- Autopilot phase rotation adds `mentions` (round-robin grows to 5 phases; publish still excluded).
- Scheduler cron: every 30 min if autopilot disabled (config-gated, default on) — replies
  within the window matter.

## 3. API + UI
- `GET /api/mentions?pending=1` — normalized mentions w/ draft status.
- `POST /api/loops/mentions` — run the loop now.
- **Inbox page**: new "Mentions" section above niche replies — avatar-less rows
  (author handle, text, age chip, [draft reply] button on unhandled, link to tweet).
  Draft flows into the existing DraftCard approval flow (target chips show "mention").
  EN+AR, RTL-safe, i18n keys.

## 4. Tests (hermetic)
- Normalize/dedupe/own-skip; pending query; handled-marking exactly when draft exists;
  draft uses conversation parent (spy on LLM prompt contains parent text);
  loop integration via dryrun X + fake LLM; API shape; autopilot rotation includes
  mentions phase. ~11 tests → aim ~161. No network, suite stays green.

Hard rules: hermetic; reads + drafts only — publish still requires human approval
(or autopilot auto-approve which stays default OFF); ONE new module + minimal touches.
