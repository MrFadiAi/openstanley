# OpenStanley v0.3 — API contract (backend ⇄ frontend)

Single source of truth for the HTTP surface. The React app (`web/`) is built
against this document; the FastAPI backend (`openstanley/server/__main__.py`)
implements it. All bodies are JSON unless noted. All times are local ISO strings
(`YYYY-MM-DDTHH:MM:SS`).

## Core shapes

### Draft (returned by every drafts/calendar/queue endpoint)

```jsonc
{
  "id": 12,
  "idea_id": 3,
  "kind": "post",              // "post" | "reply" | "quote"
  "text": "...",
  "thread": ["...", "..."],     // array | null
  "status": "draft",           // draft|approved|rejected|published|failed
  "temperature": "safe",       // safe|bold|experimental|chat
  "scheduled_at": "2026-08-19T09:00:00",   // nullable
  "x_id": null,                // set after publish
  "published_at": null,
  "created_at": "2026-08-18T10:00:00",
  "image": "media_1692.png",   // media filename | null  (served at /api/media/{name})
  "quote_of": {                // null unless kind="quote"
    "x_id": "1790123456789",
    "url": "https://x.com/user/status/1790123456789",
    "text": "quoted tweet text...",
    "author": "user"
  },
  "language": "en",            // "ar" | "en" | "mixed"
  "meta": {
    "alg": {                   // algorithm score — present on all drafts
      "score": 78,             // 0-100
      "grade": "good",         // excellent(>=80)|good(>=65)|fair(>=50)|weak
      "factors": [             // ordered by |impact|
        {"name": "Reply invitation", "impact": 12, "note": "ends with direct question"},
        {"name": "Engagement bait", "impact": -8, "note": "\"RT if\" pattern"}
      ]
    },
    "voice_match": 82,         // 0-100 vs style profile | null before scan
    "reply_to_x_id": "179...", // reply drafts: target tweet
    "target_author": "naval",  // scheduled replies: who we're replying to
    "target_score": {          // v0.3.8 engage gate — quality of the reply TARGET
      "score": 78,             // 0-100 composite (weights in [agent.engage_gate])
      "verdict": "rising",     // fresh | rising | warm | stale
      "age_h": 2.1,            // target tweet age at scoring time | null
      "components": {"recency": 0.95, "traction": 0.82, "author": 0.5,
                     "crowding": 1.0, "fit": 0.33},
      "reasons": ["age 2.1h — cooling"]
    },
    "engagement_id": 4,        // reply drafts from mentions
    "idea_title": "...", "source": "..."   // provenance
  }
}
```

### Algorithm score block

`meta.alg` — factor names (stable ids for icons): `Reply invitation`,
`Hook strength`, `Specificity & dwell`, `Scannability`, `Spam/negative-feedback risk`,
`Topic affinity`,`Timing fit`, `Media boost`, `Thread potential`, `Language quality`.
`impact` range roughly -20..+20.

## Endpoints

### Health / loops / data (unchanged from v0.2)

- `GET /api/health` → `{ok, mode, time}`
- `POST /api/loops/{name}` — name ∈ `import|study|create|engage|publish|learn|scan`
  (`scan` = deep style scan, see below) → `{ok, loop, result}`
- `GET /api/stats`, `GET /api/drafts?status=draft&limit=100`, `GET /api/ideas`,
  `GET /api/queue`, `GET /api/log?limit=60`, `GET /api/analytics`, `GET /api/voice`
- `GET /api/ideas/bank` → `{count, last:{at,added,sources}}` (v0.4.3 bank health chip)
- `POST /api/ideas/replenish` → `{ran, added, sources[], bank, bank_before}`
  (v0.4.3 manual trigger of the deterministic mining chain; X reads only when
  the chain comes up short and a client is connected)
- `POST /api/strategy` (regen via `?force=true`), `GET /api/strategy`

### Chat

- `GET /api/chat/history` → `{id, role, content, ts, meta}[]` (oldest first, ≤60)
- `POST /api/chat` `{message}` → `{reply, actions:[{id,label}], tool_results:[{name,args,ok,result}], candidates:[{text,alg}]}` (non-streaming fallback)
- `POST /api/chat/stream` `{message}` → **SSE** (`text/event-stream`), events as
  `data: {json}\n\n`:
  - `{"type":"token","text":"wor"}` — incremental reply tokens (concat to render)
  - `{"type":"action","name":"schedule_draft","args":{...},"ok":true,"result":{...}}` — tool executed
  - `{"type":"done","reply_id":123,"actions":[{"id":"create","label":"Run create loop"}],"candidates":[{"text":"...","alg":{...}}]}`
  - `{"type":"error","message":"..."}`
- `POST /api/chat/draft` `{text}` → `{ok, draft_id}`

### Drafts

- `POST /api/drafts` `{text, kind?, image?, quote_of?:{url,text?,author?}, language?, scheduled_at?}`
  → `{ok, draft_id}` (manual compose)
- `POST /api/drafts/{id}/approve` `{text?, scheduled_at?}` → `{ok, scheduled_at}`
- `POST /api/drafts/{id}/edit` `{text}` → `{ok}`
- `POST /api/drafts/{id}/reject` → `{ok}`
- `POST /api/drafts/{id}/regenerate` → `{ok, new_draft_id}` (higher temperature re-roll)
- `POST /api/drafts/{id}/reschedule` `{scheduled_at}` → `{ok}` (calendar drag)
- `POST /api/drafts/{id}/attach` `{image: "media_1.png" | null}` → `{ok}` (set/clear image)
- `POST /api/drafts/{id}/quote` `{url, text?, author?}` → `{ok, quote_of}` (parses tweet id from URL)
- `POST /api/drafts/{id}/score` → `{ok, alg}` (recompute algorithm score)
- `POST /api/replies/{id}/send` → `{ok, x_id}` (immediate reply send, unchanged)

### Media

- `POST /api/media` — multipart field `file` (png/jpg/webp/gif ≤5MB)
  → `{ok, name: "media_1692_ab12.png", url: "/api/media/media_1692_ab12.png"}`
- `GET /api/media/{name}` — serves the file bytes

### Quote preview

- `GET /api/tweet?url=https://x.com/user/status/1790...` → `{x_id, text, author}`
  (cookie/api mode fetches real tweet; dry-run returns simulated preview)

### Calendar

- `GET /api/calendar` →
```jsonc
{
  "days": {"2026-08-19": [
    {"id":1, "kind":"post", "state":"scheduled", "text":"...", "time":"09:00",
     "scheduled_at":"...", "image":null, "score":78, "language":"en"}
    // state: "scheduled" (approved) | "pending" (scheduled reply awaiting approval)
    //        | "published"
  ]},
  "empty_slots": {"2026-08-20": ["09:00", "18:00"]}   // cadence gaps next 14 days
}
```

### Insights (Recharts data)

- `GET /api/insights` →
```jsonc
{
  "engagement_over_time": [{"date":"2026-08-01","impressions":1200,"engagement":48,"posts":3}],
  "best_hours":   [{"hour":9,"avg_engagement":12.4}],          // 0-23
  "hours_heatmap":[{"day":0,"hour":9,"value":12.4}],           // day 0=Mon..6=Sun
  "format_performance": [{"format":"one-liner","count":12,"avg_engagement":8.2}],
  "language_mix": [{"language":"en","count":34},{"language":"ar","count":11}],
  "summary": {"total_impressions":0,"total_engagement":0,"avg_engagement_rate":0,
              "best_post":{"text":"...","likes":10,"replies":2}}
}
```

### Style profile / scan

- `GET /api/style-profile` →
```jsonc
{
  "exists": true,
  "stats": {                     // computed locally, always present after scan
    "posts_scanned": 640, "avg_length_chars": 132,
    "sentence": {"avg": 12.4, "p50": 9, "p90": 24},
    "punctuation": {"excl_per_post":0.2,"question_per_post":0.6,"ellipsis":0.1,"emdash":0.3,"colon":0.2},
    "emoji": {"per_post":0.4,"top":["🔥","😅"]},
    "hashtags": {"per_post":0.2,"pct_with":0.15},
    "casing": {"pct_lowercase_start":0.8,"pct_allcaps_word":0.02},
    "formatting": {"pct_multiline":0.3,"avg_line_breaks":0.7,"thread_pct":0.05},
    "vocabulary": {"top_terms":["ship","build","taste"],"uniqueness":0.62},
    "topics": ["building in public","AI agents"],
    "posting_times": {"histogram":[3,1,4,...],"best_hours":[9,13,18]},
    "language_mix": {"ar":0.3,"en":0.6,"mixed":0.1},
    "humor_markers_per_post": 0.2
  },
  "human_summary": "…",           // LLM-written paragraph
  "updated_at": "..."
}
```
- `POST /api/loops/scan` → deep scan (≤800 posts+replies, rate-limit safe)

### Settings

- `GET /api/settings` → v0.2 fields **plus** `language: "en"|"ar"`
- `POST /api/settings` accepts `{language}` (persisted; UI strings switch)

### X connect

- unchanged: `POST /api/x/cookie-connect`, `GET /api/x/status`, `POST /api/x/safety`

### System smoke (v0.3.7 live self-check)

- `GET /api/system/smoke` → last report (or `{status: "never", probes: []}` before
  the first run). Shape: `{ok, status: green|amber|red|never, ms, x_reads, ran_at,
  probes: [{name, ok, ms, detail, warn}]}`
- `POST /api/system/smoke` → run fresh now; **429** within 5 min of the last run
  (rate limit via the `smoke_last_run_epoch` setting). Also runs automatically once
  at server startup (background task; disable with `XOPENSTANLEY_NO_SMOKE=1`)

### Daily digest (v0.4.2 — the agent reports to its owner)

- `GET /api/digest?day=YYYY-MM-DD` → `{day, markdown, text, stored}` — today
  builds fresh from DB/settings only (no X, no LLM); a stored day serves its
  `data/digests/<day>.md` verbatim (`stored: true`)
- `GET /api/digest/history?limit=7` → `{days: ["2026-08-19", …]}` newest first
- `POST /api/digest/send` `{day?, lang?, force=true}` → build + store +
  webhook-deliver `{text: …}` (generic JSON — Telegram relay / Discord /
  Slack-compatible). Response `{ok, day, sent, already_sent, status_code,
  error, file}`. The scheduler runs the same delivery daily at
  `[agent] digest_hour` (default 20:00, `force=false` — no double-POST per day)
- `GET /api/settings` adds `digest_webhook_url` (**masked** — Telegram bot
  URLs embed the token), `digest_webhook_set`, `digest_hour`, `digest_last_sent`;
  `POST /api/settings` accepts `digest_webhook_url` (http(s) only, `""` clears)
  and `digest_hour` (0-23, reschedules the live job)

## Frontend serving

FastAPI (port 7878) serves `web/dist` at `/` (Vite build). Dev: `npm run dev`
in `web/` with proxy `/api` → `http://127.0.0.1:7878`.
