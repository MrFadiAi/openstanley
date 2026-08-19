Now I have all the data needed. Here is the complete structured report:

---

# Local-First X/Twitter Growth Agent — Building Blocks Research

**Date:** 2026-08-18 · **Platform:** Windows 11, Python 3.11

---

## 1. Twikit (Cookie-Based X Client)

| Field | Value |
|---|---|
| **PyPI** | `twikit==2.3.3` |
| **Last GitHub release** | v2.3.1 (2025-02-06) |
| **Last commit** | 2026-03-10 (README change only) |
| **Stars** | High (popular) |
| **License** | MIT |
| **Async-only** | Yes, since v2.0.0 (sync deprecated) |

### Auth & Cookie Methods

```python
import asyncio, json
from twikit import Client

client = Client('en-US')

# Option A: Full login (first run) — saves cookies to file
await client.login(
    auth_info_1='username_or_email',
    auth_info_2='email_or_phone',    # optional second factor
    password='password',
    totp_secret='TOTP_SECRET',       # if 2FA enabled
    cookies_file='cookies.json',
    enable_ui_metrics=True,           # js2py obfuscation (reduces suspension risk)
)

# Option B: Restore from cookies (bypasses login)
with open('cookies.json', 'r', encoding='utf-8') as f:
    client.set_cookies(json.load(f))

# Option C: Programmatic cookie load
client.load_cookies('cookies.json')

# Inspect/save cookies
cookies = client.get_cookies()        # returns dict
client.save_cookies('cookies.json')
```

### Full API Surface (verified from docs)

| Method | Signature | Notes |
|---|---|---|
| `create_tweet` | `(text, media_ids, poll_uri, reply_to, conversation_control, community_id, attachment_url, edit_tweet_id)` | Full feature support incl. polls, edits, communities |
| `create_scheduled_tweet` | `(scheduled_at: int, text, media_ids) -> str` | Returns scheduled tweet ID |
| `search_tweet` | `(query, product: 'Top'\|'Latest'\|'Media', count=20, cursor) -> Result[Tweet]` | Paginated via `.next()` |
| `get_user_tweets` | `(user_id, tweet_type: 'Tweets'\|'Replies'\|'Media'\|'Likes', count=20, cursor)` | |
| `upload_media` | `(source: str\|bytes, wait_for_completion, media_type, media_category, is_long_video) -> str` | Returns media_id |
| `send_dm` | `(user_id, text)` | Basic DM support |
| `get_trends` | `('trending')` | Trending topics |
| `like_tweet` / `unlike_tweet` | `(tweet_id)` | |
| `bookmark_tweet` / `delete_bookmark` | `(tweet_id, folder_id?)` | |
| `get_bookmarks` | `(count=20, cursor, folder_id?)` | |
| `get_retweeters` | `(tweet_id, count=40, cursor)` | |
| `delete_tweet` / `delete_scheduled_tweet` | `(tweet_id)` | |
| `get_scheduled_tweets` | `() -> list[ScheduledTweet]` | |

### ⚠️ Maintenance Status & Reliability Risks (Critical)

**Twikit is fragile in 2025-2026.** The issue tracker shows a pattern of breakage with no timely fixes:

- **2026-07:** `palm-liveness scan` on login — X requires biometric verification for flagged accounts; no programmatic workaround exists (#430).
- **2026-03:** `ClientTransaction broken — Couldn't get KEY_BYTE indices` (#408, 15 comments) — X changed internal crypto parameters.
- **2026-04:** `KeyError since April 23 2026` (#425), `Can't post tweet` (#413), `Login issue` (#414) — API field changes.
- **2025-11:** `Cloudflare 403 even with fresh cookies` (#396, 15 comments) — X tightened scraping detection.
- **2025-11:** `get_latest_friends returns 404s` (#397) — Endpoint migration.

**Assessment:** Twikit works but requires constant maintenance. For a production agent, budget time for frequent breakage fixes or pin to a known-working commit and accept limited lifetime.

### Windows Quirks

- `enable_ui_metrics=True` (default since v2.3.0) uses `Js2Py` instead of Node.js — no Node dependency needed.
- Cookie file is plain JSON — no Windows path issues.
- `httpx` (underlying HTTP client) works fine on Windows.

---

## 2. Tweepy vs X-API-V2 Libraries

| | **tweepy** | **twikit** |
|---|---|---|
| **PyPI** | `tweepy==4.17.0` | `twikit==2.3.3` |
| **Last release** | 2026-07-02 | 2025-02-06 |
| **Auth** | Official API key (OAuth 1.0a / 2.0) | Cookie-based (no API key) |
| **Cost** | $100/mo (Basic tier) | Free |
| **API coverage** | Full v1.1 + v2 | Internal/undocumented API |
| **Stability** | ★★★★★ — official API, stable contracts | ★★☆☆☆ — breaks frequently |
| **Async** | `pip install tweepy[async]` | Native async |
| **Python** | 3.9–3.13 | 3.10+ |
| **DMs** | Yes (v2) | Basic |

### Tweepy v2 Code Pattern

```python
import tweepy

# OAuth 2.0 Bearer Token (read-only)
client = tweepy.Client(bearer_token='BEARER_TOKEN')

# OAuth 1.0a User Context (read + write)
client = tweepy.Client(
    consumer_key='KEY', consumer_secret='SECRET',
    access_token='TOKEN', access_token_secret='TOKEN_SECRET'
)

# Create tweet
response = client.create_tweet(text='Hello world!')

# Search (recent, max 7 days on free tier)
tweets = client.search_recent_tweets(query='python', max_results=10)

# User timeline
tweets = client.get_users_tweets(id='123456', max_results=10)
```

### Verdict

- **Budget route:** twikit (free, fragile). Accept breakage, maintain a fork.
- **Stability route:** tweepy ($100/mo minimum for write access). Worth it if agent generates revenue.
- **Hybrid:** Use tweepy for posting (reliable) + twikit for scraping/searching (free, read-only less risky than writes).
- **No other x-api-v2 Python library** competes with tweepy. The ecosystem standard.

---

## 3. LLM Provider Abstraction (OpenAI-Compatible)

### Option A: `openai` SDK with `base_url` (Recommended — Simplest)

```
pip install openai==3.2.0
```

```python
import os
from openai import OpenAI

# Reads OPENAI_API_KEY from env automatically
# Point at any OpenAI-compatible endpoint:
client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
    api_key=os.environ["OPENAI_API_KEY"],
)

response = client.chat.completions.create(
    model=os.environ.get("OPENAI_MODEL", "glm-4"),
    messages=[
        {"role": "system", "content": "You are a Twitter growth expert."},
        {"role": "user", "content": "Write a tweet about..."}
    ],
    temperature=0.8,
)
```

**Env vars pattern:**
```bash
OPENAI_BASE_URL=https://your-provider.example.com/v1
OPENAI_API_KEY=sk-...
OPENAI_MODEL=glm-4
```

### Option B: `litellm` (Heavy — full gateway)

```
pip install litellm==1.97.0   # ★56k, but 500+ deps
```

```python
import litellm

# Single call, any provider
response = litellm.completion(
    model="openai/glm-4",  # provider/model syntax
    api_key=os.environ["OPENAI_API_KEY"],
    api_base=os.environ["OPENAI_BASE_URL"],
    messages=[...],
)
```

**Overkill for local single-user tool.** LiteLLM adds cost tracking, load balancing, 100+ provider integrations — useful for multi-provider production, unnecessary here.

### Option C: `r2d4/openlm` (Lightweight alt)

```
pip install openlm   # ★368, minimal OpenAI-compatible client
```

**Recommendation:** Use **Option A** (`openai` SDK + `base_url`). It's 2 deps (openai, httpx), standard interface, and the `base_url` + env var pattern is universally supported by OpenAI-compatible providers including z.ai/GLM.

---

## 4. Voice/Style Learning Approaches

### Approach Comparison

| Approach | Complexity | Quality | Compute Cost | Best For |
|---|---|---|---|---|
| **Few-shot prompting** | ★☆☆ | ★★★☆ | $0 (a few extra tokens) | Quick start, good enough |
| **RAG of top posts** | ★★☆ | ★★★★ | Embedding costs only | Scaling, consistent style |
| **Style rubric extraction** | ★★☆ | ★★★★ | $0 (one-time LLM call) | Systematic, auditable |
| **Fine-tuning** | ★★★ | ★★★★★ | $50–500+ | Brand accounts, high volume |

### A. Few-Shot from Past Posts (Start Here)

```python
def build_style_prompt(top_tweets: list[str], n_examples: int = 5) -> str:
    examples = "\n\n".join(
        f"Tweet {i+1}:\n{t}" for i, t in enumerate(top_tweets[:n_examples])
    )
    return f"""Here are some of my best-performing tweets. Match this exact voice:
- Sentence structure patterns
- Emoji usage (or lack thereof)
- Humor style, formality level
- Topic framing and hooks
- Hashtag patterns

{examples}

Write a new tweet in this exact style about: {{topic}}"""

# Usage
system_msg = build_style_prompt(top_tweets)
```

### B. Style Rubric Extraction (LLM Analyzes Your Voice)

```python
STYLE_RUBRIC_PROMPT = """Analyze these top-performing tweets and extract a detailed voice rubric:
- Tone (formal/casual/sarcastic/warm/technical)
- Sentence length distribution
- Punctuation patterns
- Emoji frequency and types
- Opening hook patterns (question? bold claim? story?)
- Closing patterns (CTA? question? mic drop?)
- Vocabulary level and jargon usage
- Topic domains and framing angles
- Line break / formatting habits

Output a structured rubric in JSON."""

# Run once, save as style_rubric.json, inject into system prompt
```

### C. RAG of Best Posts (Scalable)

```python
# 1. Embed top posts at setup
from openai import OpenAI
client = OpenAI(base_url=..., api_key=...)

posts = load_top_posts()  # from SQLite or file
embeddings = client.embeddings.create(
    input=posts,
    model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
)
# Store (post, embedding) in SQLite or FAISS

# 2. At generation time, retrieve similar-style posts
def get_style_examples(topic: str, n: int = 3) -> list[str]:
    topic_emb = client.embeddings.create(input=[topic], model=...).data[0].embedding
    # cosine similarity against stored embeddings -> top N
    ...

# 3. Inject as few-shot examples
```

### D. Fine-Tuning (Not Recommended for Single User)

Requires 500+ high-quality posts minimum. Cost: $50–500 depending on model. Only worth it for high-volume brand accounts. Use `openai` SDK's fine-tuning endpoints if the provider supports it.

**Recommendation:** Start with **B (rubric extraction) + A (few-shot)**. Add C (RAG) when you have 50+ posts. Skip D.

---

## 5. Local Web Dashboard Stack

### Comparison for Single-User Local Tool

| Stack | Deps | Dev Speed | Runtime | Verdict |
|---|---|---|---|---|
| **FastAPI + htmx + Jinja2 + TailwindCSS** | 4 pip packages | ★★★★★ | Single `uvicorn` process | ✅ **Recommended** |
| Next.js | npm, node_modules, React | ★★★☆ | Node server + build | Overkill for local single-user |
| Plain HTML + fetch | 0 deps | ★★★☆ | Static files via FastAPI | Too manual for interactive UI |
| Streamlit | 1 pip | ★★★★ | Auto-reload | Good for quick proto, limited customization |

### Recommended: FastAPI + htmx

```
pip install fastapi==0.141.1 uvicorn[standard] jinja2 python-multipart
```

**Reference project:** `volfpeter/fastapi-htmx-tailwind-example` (★108)

```python
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Query SQLite for queue, analytics, etc.
    queue = db.get_pending_tweets()
    stats = db.get_analytics()
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "queue": queue, "stats": stats,
    })

@app.post("/tweet/{id}/approve")
async def approve_tweet(tweet_id: int):
    db.update_status(tweet_id, "approved")
    # htmx returns partial HTML — no full page reload
    return templates.TemplateResponse("queue_row.html", {
        "request": {}, "tweet": db.get_tweet(tweet_id),
    })
```

**Template (Jinja2 + htmx + Tailwind via CDN):**
```html
{% extends "base.html" %}
{% block content %}
<div class="grid grid-cols-3 gap-4">
  <div>
    <h2 class="text-xl font-bold">Queue ({{ queue|length }})</h2>
    <div id="queue-list" hx-get="/queue/partial" hx-trigger="load">
      <!-- htmx loads partial HTML here -->
    </div>
  </div>
  <div>
    <h2 class="text-xl font-bold">Analytics</h2>
    <p>Impressions: {{ stats.impressions }}</p>
    <p>Engagement: {{ stats.engagement_rate }}%</p>
  </div>
</div>
{% endblock %}
```

**Why this wins:** No Node.js needed, no build step, hot-reload via `uvicorn --reload`, htmx handles all interactivity with server-rendered partials. Tailwind via CDN for styling. Single Python process.

---

## 6. Scheduling in Python

### APScheduler 3.11.3 (Recommended)

```
pip install apscheduler==3.11.3
```

| | APScheduler 3.x | APScheduler 4.x | Simple Loop |
|---|---|---|---|
| **Status** | ✅ Stable, PyPI released | ❌ Never released on PyPI | Always works |
| **Cron support** | Yes | Planned | Manual |
| **Persistency** | SQLAlchemy/SQLite job store | Planned | Manual |
| **Async** | `AsyncIOScheduler` | Native async | Natural |
| **Last release** | 2026-06-28 | — | — |

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

scheduler = AsyncIOScheduler(
    jobstores={
        'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
    },
    job_defaults={'coalesce': True, 'max_instances': 1},
)

# Schedule tweet posting
scheduler.add_job(
    post_next_in_queue,
    'cron',
    hour='9,12,15,18',   # 4 tweets/day at optimal times
    minute='0',
    id='post_tweet',
    replace_existing=True,
)

# Schedule engagement scan
scheduler.add_job(
    scan_for_opportunities,
    'interval',
    hours=1,
    id='engagement_scan',
)

scheduler.start()
```

**Windows quirk:** APScheduler 3.x works fine on Windows. `AsyncIOScheduler` is the right choice for an async app. No known Windows-specific issues.

**Simple loop alternative** (even simpler, no extra dep):
```python
import asyncio

async def main_loop():
    while True:
        await post_next_in_queue()
        await scan_for_opportunities()
        await asyncio.sleep(3600)  # hourly

asyncio.run(main_loop())
```

**Recommendation:** Use **APScheduler 3.11.3** for cron expressions and persistent job state. If you want zero deps, the simple loop works but loses cron flexibility.

---

## 7. SQLite for State

```python
# Sync (stdlib, zero deps)
import sqlite3
conn = sqlite3.connect('agent_state.db')
conn.row_factory = sqlite3.Row  # dict-like access

# Async (recommended for async app)
pip install aiosqlite==0.22.1

import aiosqlite

async def init_db():
    async with aiosqlite.connect('agent_state.db') as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                status TEXT DEFAULT 'draft',  -- draft, queued, approved, posted, failed
                scheduled_at TIMESTAMP,
                posted_at TIMESTAMP,
                tweet_id TEXT,
                impressions INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                retweets INTEGER DEFAULT 0,
                replies INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS style_posts (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                impressions INTEGER,
                engagement_rate REAL,
                collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS analytics (
                date TEXT PRIMARY KEY,
                followers INTEGER,
                impressions INTEGER,
                engagement_rate REAL
            );
            CREATE TABLE IF NOT EXISTS engagement_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                target_tweet_id TEXT,
                target_user_id TEXT,
                result TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()
```

**Why SQLite:** Single-user local tool, zero setup, files live next to the app, easily inspectable with any SQLite viewer. No Docker, no Postgres. `aiosqlite` is a thin async wrapper — no issues on Windows.

---

## 8. OSS Agents & References Worth Studying

### `SoCloseSociety/MiloAgent` (★31, Python, Active — **Best Architecture Reference**)

**The closest thing to what you're building.** Multi-platform growth agent (Reddit primary, Twitter/Telegram "learning"). Key architectural patterns to adopt:

- **Orchestrator + APScheduler** cycle: Scan → Score → Think → Write → Check → Post → Learn
- **Self-learning engine:** Performance weighting, A/B testing, prompt evolution, sentiment analysis
- **Safety system:** Rate limiting per-account, human timing (random delays/jitter), shadowban detection, circuit breaker, content validation, CAPTCHA detection
- **Zero-cost LLM stack:** Groq → Gemini → Ollama failover chain
- **Dashboard:** FastAPI web + Terminal TUI + Telegram bot
- **Config:** YAML template/override pattern (`config/*.yaml` → `config/*.local.yaml`)

```
git clone https://github.com/SoCloseSociety/MiloAgent.git
```

### `romeoscript/agent-twitter-client` (★81, TypeScript — **Cookie-Based X Client Ref**)

Fork of the defunct `elizaOS/agent-twitter-client` (original repo 404'd). TypeScript, cookie-based, no API key needed. Full API: search, post, DM, follow, trends, retweet, profile, relationships.

```
npm install agent-twitter-client
```

**Worth reading** for: cookie management patterns, the `Scraper` class design, authentication flow with cookie persistence. Even if you build in Python, the interface design is instructive.

### `xai-org/x-algorithm` (★31,827, Rust — **X's Actual For You Algorithm**)

Released 2025-05-15 under Apache-2.0. This IS the production algorithm. Critical for understanding what X actually ranks:

- **Ranking function:** `S = γ · m(k) · φ( Σ wₐ Pₐ )` — weighted sum of model-predicted action probabilities
- **22 weighted signals** for engagement prediction
- **Author diversity decay** — penalizes same-author repetition in feed
- **Out-of-network gates** for replies and retweets
- **Phoenix transformer** — the actual ML model (black box weights, inspectable architecture)
- **Key insight:** The learned model dominates; the explicit weights blend action value with typical base rates

### `julienaxyomlabs/x-algorithm-notes` — **Equation-Level Analysis**

Written-out ranking function with full constants, verified against source code line-by-line. Includes:
- `ANALYSIS.md` — 10 findings with file:line citations
- `EQUATION.md` — Complete ranking-stage scoring function
- `x-ranking-model.xlsx` — Live spreadsheet with all 214 calculations
- Key finding: "a table of common advice the code contradicts"

**Actionable takeaway:** Build tweet scoring heuristics from the weight structure. High-value signals: replies from mutual follows (boosted), out-of-network replies (gated), engagement-bait labeling chain.

### `kkkhushman/x-algo-skill` — **Portable Agent Skill for X Algorithm**

Agent skill (SKILL.md format) encoding deep X algorithm knowledge with file:line citations. Portable across Claude Code, Cursor, Codex, etc. Could inform your agent's tweet optimization logic.

### `Freespirits/social-auto-engine` (★19, Python, Active)

Multi-channel social media platform (Facebook, Instagram, LinkedIn, TikTok, X). AI content generation, approval workflows, 17 content skills. Dashboard included. Worth studying for: multi-platform patterns, content approval workflow, dashboard design.

### OpenClaw Twitter Plugins

The OpenClaw skills in this Hermes instance include general-purpose tools (identity, posting, wallet) but **no Twitter-specific plugin** was found in the available skill set. The `nostr` skill exists for Nostr protocol posting — architecturally similar pattern could be applied to X.

---

## 9. Recommended Stack (Summary Table)

| Layer | Library | Version | Install |
|---|---|---|---|
| **X Client (free)** | `twikit` | 2.3.3 | `pip install twikit` |
| **X Client (paid, stable)** | `tweepy` | 4.17.0 | `pip install tweepy[async]` |
| **LLM Client** | `openai` | 3.2.0 | `pip install openai` |
| **LLM Gateway (optional)** | `litellm` | 1.97.0 | `pip install litellm` |
| **Web Framework** | `fastapi` | 0.141.1 | `pip install fastapi uvicorn[standard]` |
| **Templates** | `jinja2` | (stdlib with FastAPI) | bundled |
| **Frontend Interactivity** | `htmx` | CDN | `<script src="https://unpkg.com/htmx.org">` |
| **CSS** | Tailwind CSS | CDN | `<script src="https://cdn.tailwindcss.com">` |
| **Scheduling** | `apscheduler` | 3.11.3 | `pip install apscheduler` |
| **Database** | `aiosqlite` | 0.22.1 | `pip install aiosqlite` |
| **HTTP (underlying)** | `httpx` | 0.28.1 | (twikit dep, also install standalone) |
| **Async Runtime** | `asyncio` | (stdlib) | built-in |

### Minimal `requirements.txt`

```
twikit==2.3.3
openai==3.2.0
fastapi==0.141.1
uvicorn[standard]
jinja2
python-multipart
apscheduler==3.11.3
aiosqlite==0.22.1
httpx==0.28.1
```

### Architecture Sketch

```
┌─────────────────────────────────────────────┐
│              FastAPI + htmx Dashboard        │
│         (Jinja2 templates, Tailwind CDN)      │
├─────────────────────────────────────────────┤
│              APScheduler Orchestrator         │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│  │  post_   │ │  engage_ │ │  analytics_  │ │
│  │  job     │ │  scan    │ │  collect     │ │
│  └────┬─────┘ └────┬─────┘ └──────┬───────┘ │
├───────┼────────────┼───────────────┼─────────┤
│       │     LLM Engine (openai SDK)          │
│       │     base_url → any OpenAI-compat     │
├───────┼────────────┼───────────────┼─────────┤
│       │     Style System                      │
│  ┌────▼─────┐ ┌────▼─────┐ ┌─────▼───────┐  │
│  │ rubric   │ │ few-shot │ │ RAG top     │  │
│  │ JSON     │ │ examples │ │ posts (emb) │  │
│  └──────────┘ └──────────┘ └─────────────┘  │
├───────┼────────────┼───────────────┼─────────┤
│       │     X Client Layer                    │
│  ┌────▼────────────────────────────────────┐ │
│  │  twikit (primary, free)                  │ │
│  │  tweepy (fallback for critical posts)    │ │
│  └─────────────────────────────────────────┘ │
├─────────────────────────────────────────────┤
│              SQLite (aiosqlite)               │
│  drafts │ queue │ analytics │ style_posts     │
│  engagement_log │ jobs                         │
└─────────────────────────────────────────────┘
```

### Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Twikit breaks (likely) | Pin commit, maintain fork, add retry with exponential backoff, circuit breaker |
| Cloudflare 403s | Rotate cookies frequently, add random delays, consider proxy rotation |
| Palm liveness scan | Manual unlock required; detect and pause agent, notify via dashboard |
| LLM hallucination in tweets | Mandatory human approval step in dashboard before posting |
| Rate limiting | Per-account daily caps (MiloAgent's karma-tier system is a good model) |
| Account suspension | Shadowban detection (profile visibility check), multi-account rotation if needed |