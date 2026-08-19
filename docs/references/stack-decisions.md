# Stack Decisions
> Machine: Windows 11, Ryzen 7 5700U, 30GB RAM. Python 3.11.15. Local single-user use.

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11 | Best X libs + LLM SDKs + scheduler ecosystem |
| X client | `xphone` interface: XApiClient (official v2) OR TwikitClient (cookie, free) | Free tier of official API is nearly useless for reads; twikit gives full read/write without paid API |
| LLM | OpenAI-compatible provider (GLM via z.ai, OpenAI, or any base-url+key) | Provider-agnostic; user already runs GLM |
| Server | FastAPI + Uvicorn | Async, native to Python, one process |
| Frontend | Single-file vanilla JS dashboard (no build step) | Local tool, zero node_modules, instant |
| Scheduler | APScheduler (AsyncIOScheduler) | Cron-style jobs inside the server process |
| DB | SQLite (data/xstanley.db) | Zero-config durable state |
| Config | data/config.toml + .env for keys | Simple, human-editable |

## X access strategy (three modes)
1. **Official API v2** (if user has Basic+): OAuth2 PKCE, media upload, full compliance. ~$200/mo Basic.
2. **twikit (cookies)**: free, reads timeline/search/mentions + posting. Risk: unofficial, account flagging possible. Recommended default for local personal use, moderate rate.
3. **Dry-run mode**: no X connection at all — full pipeline runs against local data. Safe first-run default.
