# Architecture

```
┌────────────────────────────  FastAPI (127.0.0.1:7878)  ───────────────────────────┐
│  REST /api/*          web/index.html (vanilla JS dashboard, polls 15s)            │
│  APScheduler: study 03:00 · create 07:00 · engage :30 · publish */10 · learn Sun  │
└──────────┬───────────────────────────────────────────────────────────────────────┘
           │
      Agent (gen/agent.py) — the five loops, one XClient injected
           │
  ┌────────┼─────────────────────────────┬──────────────────────────┐
  │        │                             │                          │
  ▼        ▼                             ▼                          ▼
voice.py  ideas.py                   drafts.py / replies.py      x/client.py
(style     (story bank:               (LLM ghostwriting,         XDry | XCookie
rubric +   niche winners +            temperature ladder,        (twikit) | XApi
few-shot)  evergreen mix)             approval-gated)            (official v2)
  │        │                             │                          │
  └────────┴─────────────┬───────────────┘                          │
                         ▼                                          │
                gen/llm.py — provider-agnostic chat()               │
                (openai | anthropic transport, httpx, retries)      │
                         │                                          │
                         ▼                                          ▼
              LLM endpoint (GLM-5.3 @ z.ai)                X platform
```

All state in one SQLite DB (`data/openstanley.db`, WAL mode, single-writer lock):
posts / ideas / drafts / engagements / voice_profile / settings / agent_log.

## Key decisions
- **Approval gate as a DB state machine**: draft → approved(+scheduled_at) → published.
  The publish loop only picks approved rows whose slot has passed. Dry-run mode makes
  `post_tweet` a log line, so the same code path is exercised safely.
- **Voice = rubric + few-shot**, rebuilt weekly from top-engagement posts. Rubric is a
  structured JSON fingerprint (diction/tone/structure/signature_moves/do_not) injected
  into every generation prompt.
- **Idea bank decouples study from create**: nightly study keeps ≥16 fresh ideas scored
  0–10; daily create consumes them (40% niche-react / 35% evergreen / 25% remix mix
  encoded in the prompt).
- **Three X modes behind one interface** so the product works free (dryrun/cookie) and
  compliant (api) without touching business logic.
- **Blocking LLM calls** (httpx) inside `asyncio.to_thread` — simple, and a single-user
  local tool doesn't need streaming or concurrency.

## Failure handling
- LLM: 2 retries with backoff; failed drafts logged to agent_log (level=error), loop continues.
- X errors during publish mark the draft `failed` (visible in dashboard, re-approvable).
- Transient DB concurrency handled by a process-wide lock + WAL.
