# OpenStanley v0.3 Upgrade Brief

You are upgrading OpenStanley (D:\ai\openstanley) — a local-first clone of getstanley.ai
(AI Head of Content for X), Python 3.11 venv at .venv, FastAPI backend + vanilla-JS
dashboard at http://127.0.0.1:7878, SQLite at data/openstanley.db, real GLM-5.3 via z.ai
anthropic transport (key in .env as XOPENSTANLEY_LLM_API_KEY — NEVER read .env into git,
NEVER touch the X auth token in .env, NEVER make real X API calls in tests).

CURRENT STATE (v0.2): chat-first "Ask OpenStanley" UI (Write/Calendar/Inbox/Ideas/Strategy/
Insights/Connect/Settings/Log tabs), content calendar, strategy one-pager, safety caps
for cookie mode, APScheduler loops, two passing test suites (tests/test_smoke.py,
tests/test_e2e_fake_llm.py — run with `.venv/Scripts/python tests/test_smoke.py`).

## THE 7 UPGRADES — in priority order

### 1. X Algorithm Intelligence (the core differentiator)
Integrate the open-source X ranking model knowledge from https://github.com/twitter/the-algorithm
(the actual ML ranking code X open-sourced). Build `openstanley/gen/algorithm.py` — a scoring
engine that predicts how the X algorithm will treat a draft:
- Signal weights derived from the repo: reply-likelihood, dwell time, negative-feedback
  probability, author-author graph affinity, simclusters interest match
- Score every draft 0-100 ("Algorithm Score") with factor breakdown (what's helping,
  what's hurting)
- Feed these factors into generation prompts so drafts are born algorithm-fit
- Show the score + breakdown on every draft card in the UI
Reference material already in repo: docs/references/x-api-landscape.md has current X
pricing/limits; docs/references/competitive-landscape-2026.md has 2025-26 growth tactics.
Do NOT clone the twitter repo — distill its ranking signals into a lightweight local
scoring model (rule-based + LLM-hybrid is fine; no torch).

### 2. Bilingual: Arabic + English
The account owner may post in Arabic, English, or mixed. Add full i18n:
- All prompts support bilingual voice: detect language of account posts, learn the
  voice in both languages if both present
- Drafts can be requested in Arabic or English (chat: "write a post in Arabic about X")
- UI strings bilingual with a language toggle (AR/EN) — RTL layout for Arabic
- Arabic posts must follow X Arabic conventions: proper حاء handling, no weird
  numerals (Arabic-Indic ٠١٢ vs Western — match account style), RTL punctuation
- The chat agent itself responds in the user's language automatically

### 3. Learn Connected Account Style (deep scan)
The "scan" function must produce a rich style profile:
- Pull up to 800 recent posts + replies of the connected account (batched, rate-limit
  safe, respecting safety caps)
- Extract: vocabulary, sentence length distribution, punctuation habits, emoji usage,
  hashtag usage, casing, formatting patterns (line breaks, threads), humor/sarcasm
  frequency, topic distribution, posting time distribution, language mix
- Store as structured `style_profile` (JSON) + human-readable summary
- Voice learning uses style_profile + rubric together; drafts must be checked
  against style_profile before finalizing (a "voice-match" pass)

- Scheduled replies: engage loop finds target posts (from niche accounts, matched by
  niche relevance) and drafts replies that are scheduled, not sent immediately
- All scheduled items visible on the Content Calendar with type badges (post/reply/quote)

### 4. Scheduling: posts with images, scheduled replies, quote posts
- Drafts can carry an image (upload in UI, stored in data/media/, referenced by
  filename; upload endpoint POST /api/media + serve via GET /api/media/{name})
- Scheduled posts with images publish with the image attached
- Quote posts: draft can reference another tweet (quote_of field in drafts table +
  UI: paste tweet URL → preview text + "quote this" button; generation can propose
  quote-post angles from important announcements in niche)
- Scheduled replies: engage loop drafts replies to niche-relevant targets; they go
  through the same approval + scheduling flow as posts (kind='reply' with scheduled_at)

### 5. Better UI — most powerful library
Replace vanilla-JS dashboard with **React + Vite + TypeScript + Tailwind CSS + shadcn/ui
+ Lucide icons + Recharts** (charts) + **framer-motion** (micro-interactions):
- Keep the existing visual identity (dark theme, purple accent #7c6cff, same sidebar
  structure) but production-grade polish: command palette (cmd+k), toasts, skeletons,
  keyboard shortcuts, drag-to-reschedule on calendar (dnd-kit), compact/dense mode
- Calendar: month + 2-week views, drag-to-reschedule (dnd-kit), per-item type badges,
  "empty slot" hints where strategy cadence suggests a post
- Chat: streaming responses (SSE from /api/chat/stream), markdown rendering, quote
  blocks, action chips, per-message "save as draft" button on every OpenStanley reply
  containing a post candidate
- Draft cards: Algorithm Score badge (colored 0-100), factor breakdown popover,
  voice-match %, image attachment preview, edit/approve/discard, "regenerate" (re-rolls
  with higher temperature)
- Insights: Recharts dashboards (engagement over time, best posting hours heatmap,
  format performance, language mix pie)
- AR/EN language toggle in settings + RTL support
- Serve the built frontend from FastAPI (web/ → static mount of web/dist), dev proxy OK

### 5b. STREAMING CHAT (required for #5)
`POST /api/chat/stream` — SSE endpoint streaming tokens as they arrive from z.ai.
Modify gen/llm.py `chat()` to expose `chat_stream(cfg, system, user) -> Iterator[str]`
(yield tokens; anthropic transport: stream SSE from api.z.ai/api/anthropic with
"stream": true). Chat UI renders tokens progressively. Keep non-streaming chat() for
loops. This is how getstanley.ai feels fast — do not skip it.

### 6. Agent capabilities (chat actions)
OpenStanley chat can now DO things, not just say:
- "schedule this for 9pm" / "post at 6" → creates scheduled draft with time
- "quote @user's post about Y" → finds the tweet, creates quote draft
- "show me my best post this week" → reads analytics
- "what should I post today" → idea bank + algorithm score + voice match, picks top
- Implement as intent detection → tool calls (function calling style: define tools in
  prompt, LLM returns JSON action blocks, backend executes, result feeds back)
- Tool registry in openstanley/gen/tools.py: schedule_draft, create_quote_draft,
  query_analytics, pick_idea, scan_account, regenerate_draft — each executes locally

registry in openstanley/gen/tools.py: schedule_draft, create_quote_draft,
query_analytics, pick_idea, scan_account, streaming chat uses it too

### 7. Testing + verification
- Keep both suites green; add tests for: algorithm scoring (deterministic inputs →
  expected score ranges), bilingual voice match (Arabic sample → Arabic-style output
  mock), scheduling with images, quote-post flow, tool-call intent parsing
- E2E: full loop in dry-run with fake LLM covering post w/ image + quote post +
  scheduled reply + algorithm score on all
- NO real X calls in tests; NO real posting; approval gate must hold in all tests
- Run: `.venv/Scripts/python -m pytest tests/ -x -q` if pytest installed, else run
  each suite directly

## CONSTRAINTS
- Windows 11, Git-Bash paths (/d/ai/openstanley), Python 3.11 venv (.venv/Scripts/python)
- LLM: z.ai anthropic transport ONLY (https://api.z.ai/api/anthropic), model glm-5.3,
  key from env XOPENSTANLEY_LLM_API_KEY. GLM-5.3 has NO vision — never send images to it.
  For images use alt-text based reasoning only.
- Keep FastAPI single-port (7878) serving API + built frontend
- No new heavy deps beyond: React, Vite, TS, Tailwind, shadcn/ui, Lucide, Recharts,
  framer-motion, dnd-kit, pytest (all local, npm install in web/ or repo root as you decide)
- After ALL work: run both test suites, restart server, curl /api/health, verify
  dashboard loads at http://127.02.1:7878 (typo-safe: http://127.0.0.1:7878), report
  what you built + test results
- Git commit at logical milestones (local repo, no remote)

## WIN CONDITION
A production-feel local app where: you paste cookies → deep-scan your account →
OpenStanley knows your bilingual voice + niche → drafts algorithm-optimized posts (with
images/quotes/scheduled replies) → you approve → it schedules and publishes on
schedule. Feels as fast and smart as getstanley.ai, but yours.
