# OpenStanley

**Local-first AI Head of Content for X** — a self-hosted, open clone of
[getstanley.ai](https://www.getstanley.ai)'s content operation. An agent that
studies your niche, writes in your voice, schedules around your best hours,
answers mentions, learns from what works — and never publishes a single post
without your tap.

```
autopilot drafts → card lands on your phone (with image + one-tap approve/reject)
→ you tap → smart slot assigned → publish loop ships it through safety caps
```

## Why it exists

You shouldn't have to open a dashboard to run your account. OpenStanley works
from **Telegram** (voice notes, one-tap approvals, live cards) or the web
dashboard — the AI does ~95% of the work, you do the ~5% only a human should:
deciding what represents you.

## Features

- **✍️ Write** — streaming chat; post candidates with Algorithm Score + voice
  verdicts; one-click **Save as draft**; the agent *acts* (schedule, quote,
  search) via a tool registry
- **🎙 Voice notes** — send a voice message on Telegram, it's transcribed
  locally (Whisper, CPU) and drafted from your words
- **🔎 Live search** — `web_search` (DuckDuckGo), `x_search` / `x_trends`
  (through your cookie session, **no paid X API**), and `trend_post` which
  drafts from real findings, never invention
- **🧵 Threads** — `/thread <topic>` composes a 3–6 tweet chain; one approval,
  publishes chained
- **✨ Steal this hook** — hook patterns mined from your niche's top posts,
  remixable into fresh in-voice drafts
- **🖼 Auto media** — eligible posts get a branded typographic card rendered
  offline (the media boost, no stock photos)
- **🧠 Brain** — a self-maintaining memory (rules, strategies, journal) that
  learns from every publish and feeds every prompt; the agent cites its own
  learned data when asked
- **📅 Calendar** — queue rail, slot-based day columns (your posting times),
  smart-slot scores, collision-aware spreading, drag-to-reschedule
- **📥 Inbox** — approval-gated drafts with Algorithm Score, voice match,
  images, quote previews; **nothing ships without you**
- **📈 Insights** — year impressions heatmap, growth + delta, engagement
  orbit, gamified milestones, best-content-to-repost wall — all from your
  real data
- **🤖 Autopilot** — study → create → engage → mentions → learn on a 45-min
  beat; publish is **never** autonomous
- **🛡 Safety** — 4 posts / 10 replies per day, human-like jittered delays,
  over-cap drafts auto-reschedule to the next free slot
- **🌍 Bilingual** — Arabic + English everywhere (voice rubrics, convention
  checks, RTL UI); replies mirror your language
- **📱 Telegram = second frontend** — one-tap approve/reject cards that
  rewrite live (showing each draft's slot), `/status /ideas /drafts /digest
  /study /account /img /post /thread`, photos, voice notes
- **👤 Multi-account** — several X accounts, each with its own brain, caps,
  metrics, and archive-on-delete

## Quick start

```bash
git clone https://github.com/YOUR_USER/openstanley.git
cd openstanley

# 1. Python deps
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        # Windows
# pip install -r requirements.txt                    # macOS/Linux

# 2. Secrets — the only required key is the LLM key
copy .env.example .env                               # then edit .env
#   macOS/Linux: cp .env.example .env

# 3. Frontend (first run or after web/ changes)
cd web && npm install && npm run build && cd ..

# 4. Run
.venv\Scripts\python -m openstanley.server           # Windows
# python -m openstanley.server                       # macOS/Linux
# → open http://127.0.0.1:7878
```

Ships in **dry-run mode** (simulated X, real LLM): the whole pipeline works
with zero X credentials and posts nothing.

### The LLM key (required)

Any OpenAI- or Anthropic-compatible endpoint. The tested default is **GLM via
z.ai** — get a key at [z.ai](https://z.ai), put it in `.env` as
`OPENSTANLEY_LLM_API_KEY`. Model/base URL live in `data/config.toml`.

### Connecting a real X account (optional)

Dashboard → **Connect** tab (one-click wizard):

1. Log in to x.com in your browser
2. F12 → Application → Cookies → `https://x.com`
3. Copy the **`auth_token`** value (and **`ct0`** — recommended)
4. Paste into the wizard's two fields → **Connect & validate**

| mode | how | cost | notes |
|---|---|---|---|
| `dryrun` | nothing to do | free | simulated timeline/mentions/posts |
| `cookie` | wizard above (twikit) | free | unofficial — ToS risk; caps keep you human-like |
| `api` | official X API v2 keys in `.env` | pay-per-use | fully compliant |

**Durable cookies (auto-heal):** X rotates cookies (~daily). Start Brave once
with `brave.exe --remote-debugging-port=9222` (logged in to x.com); on any auth
failure OpenStanley pulls fresh cookies read-only via CDP and rebuilds — no
human in the loop. Details in the Connect tab.

### Telegram bot (optional, recommended)

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Dashboard → **Settings → Telegram** → paste token → enable
3. Send your bot `/start` — it bootstraps your chat into the whitelist

Then everything runs from your phone: approval cards with one-tap buttons,
voice notes, `/thread`, `/digest`, live search.

## Configuration

`data/config.toml` — LLM model/base_url, X mode, posting times, niche
accounts, safety caps, autopilot interval. All editable from Settings in the
dashboard. Secrets stay in `.env` (never committed).

## Tests

```bash
.venv\Scripts\python -m pytest -q      # 430+ tests, hermetic (no network, no real X)
```

## Project layout

```
openstanley/
  core/       config, SQLite db, safety caps, text hygiene
  gen/        the mind: brain, voice+lock, drafts, ideas, hooks, engage gate,
              slots, metrics, insights, quote cards, voice notes, web search,
              threads, chat + tool registry, LLM client
  x/          three X clients (dryrun / cookie / api), cookie auto-heal,
              twikit compatibility patches
  integrations/  telegram frontend (poller, cards, buttons, voice)
  server/     FastAPI app + scheduler (all loops)
web/          React + TypeScript dashboard
tests/        hermetic suite
```

## Design principles

- **The approval gate is architectural** — no code path auto-publishes; the
  human tap is the only key
- **Local-first** — one SQLite file, your cookies and keys never leave your
  machine, Whisper runs on CPU, search needs no paid API
- **Honest data** — Insights shows zeros when the account is young; nothing is
  fabricated
- **Production follows approval** — the create loop throttles when your queue
  is deep, drafts expire after 3 days unapproved

## License

MIT
