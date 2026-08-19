# OpenStanley

**Local-first AI Head of Content for X** — an open, self-hosted clone of
[getstanley.ai](https://www.getstanley.ai)'s content operation, X-only.

## The OpenStanley experience (v0.3)

- **✍️ Write** — streaming chat ("Ask OpenStanley · AI Head of Content"): tokens
  arrive live over SSE, replies mirror your language (AR/EN), post candidates
  appear as quote blocks with one-click **Save as draft**, and OpenStanley can
  actually DO things — "schedule this for 9pm", "quote @user's post",
  "what should I post today?" — via a tool-call registry.
- **🧠 Algorithm Score** — every draft is scored 0-100 by a local engine
  distilled from X's open-sourced ranking model (reply-likelihood, dwell,
  negative-feedback risk, topic affinity, timing, media, language quality)
  with a factor breakdown: what's helping, what's hurting.
- **📅 Calendar** — month + 2-week views, drag-to-reschedule, per-item type
  badges (post/reply/quote), "empty slot" hints where your cadence wants a post.
- **📥 Inbox** — approval-gated drafts with Algorithm Score badges, factor
  popovers, voice-match %, image thumbnails, quote previews. Nothing ships
  without you.
- **💡 Ideas** — scored story bank from your niche; quote-post angles from big
  niche announcements.
- **🎯 Strategy** — auto-maintained one-pager from your real data.
- **📈 Insights** — engagement over time, best-hours heatmap, format
  performance, language mix (AR/EN).
- **🔗 Connect** — cookie wizard + safety caps + **deep scan**: up to 800
  posts+replies distilled into a structured style profile (punctuation,
  emoji, casing, vocabulary, topics, best hours, language mix) — OpenStanley
  writes to match the numbers, in both languages.
- **🌍 Bilingual** — Arabic + English everywhere: per-language voice rubrics,
  Arabic convention checks (؟ ، ؛, numeral consistency, no Persian
  lookalikes), RTL UI with an AR/EN toggle.
- **🚀 Autopilot** — OpenStanley runs itself: study → create → engage → learn on
  a round-robin tick (default every 45 min, 15–90 selectable), with bounded
  jitter so the beat never looks robotic. **Publish is never on autopilot** —
  the approval gate stays yours, forever.

```
paste cookies → deep scan → bilingual voice + niche → algorithm-fit drafts
   (images / quotes / scheduled replies) → YOU approve → publish on schedule
```

## Quick start

```bash
cd D:\ai\openstanley
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install pytest python-multipart   # tests + uploads

# secrets: .env with OPENSTANLEY_LLM_API_KEY (z.ai GLM). See .env.example

# frontend (first run / after web/ changes)
cd web && npm install && npm run build && cd ..

.venv\Scripts\python -m openstanley.server
# → open http://127.0.0.1:7878
```

Ships in **dry-run mode** (simulated X, real LLM): the whole pipeline works
with zero X credentials and posts nothing. Frontend dev: `npm run dev` in
`web/` (proxies `/api` → 127.0.0.1:7878); the server serves `web/dist`.

## Connecting your real X account

Dashboard → **Connect** tab (one-click wizard):

1. Log in to x.com in your browser
2. F12 → Application → Cookies → `https://x.com`
3. Copy `auth_token` (and `ct0`) values
4. Paste as `{"auth_token":"...","ct0":"..."}` → **Connect & validate**
5. **Deep scan** → bilingual style profile + voice

| mode | how | cost | notes |
|---|---|---|---|
| `dryrun` | nothing to do | free | simulated timeline, mentions, posts |
| `cookie` | wizard above (twikit) | free | unofficial — ToS risk; caps keep you human-like; lib pinned 2.3.3 |
| `api` | official X API v2 keys in `.env` | pay-per-use | fully compliant — ~$6-10/mo typical use |

Safety layer: daily caps (4 posts / 10 replies default) + jittered 5-20s
human-like delays before every write; approved posts that hit the cap
auto-reschedule to tomorrow's first slot.

### Durable cookies (auto-heal)

X rotates cookies (`ct0` ~daily, `auth_token` on any browser re-login). When
that happens in cookie mode, OpenStanley pulls fresh ones from your live Brave
over CDP and rebuilds the client — no human in the loop:

1. Start Brave once with the debug port:
   `brave.exe --remote-debugging-port=9222` (or add the flag to your shortcut)
   and keep it open, logged in to x.com.
2. That's it. On any twikit auth failure (401/code 32, 403/code 353, KEY_BYTE
   family) OpenStanley marks the session stale, reads `auth_token`+`ct0` from
   Brave via CDP `Network.getAllCookies` (read-only), persists them to `.env`
   atomically, and rebuilds the client. Reads retry once after a heal; writes
   re-raise (never auto-retried — no double-post risk).
3. Heals are cooldown-gated (one attempt / 10 min) so an expired session can
   never cause a request loop. No browser is ever auto-launched: if Brave
   isn't listening on 9222, the heal just fails gracefully and the Connect
   tab tells you to restart Brave with the flag.

The Connect tab shows the state: **Session auto-healed ✓** (with the last
heal time) or **Cookies expired — restart Brave with
--remote-debugging-port=9222**.

## The loops

| loop | schedule | what it does |
|---|---|---|
| import | manual | pull your posts + niche posts into the DB |
| **scan** | manual (Connect tab) | deep scan ≤800 posts+replies → style profile + bilingual voice |
| study | 03:00 nightly | refresh niche winners, refill idea bank to 16+ |
| create | 07:00 daily | draft N algorithm-fit posts (safe/bold/experimental ladder) |
| engage | hourly :30 | mentions → reply drafts **+ scheduled niche replies** |
| publish | every 10 min | post approved items whose slot arrived (images/quotes/replies attached) |
| learn | Sunday 05:00 | refresh metrics, rebuild voice rubric + examples |

## Autopilot (v0.3.5)

The cron loops above are the fixed skeleton; **autopilot** (Insights tab) is
the self-driving layer on top. When enabled, one scheduler job ticks every
`[agent] autopilot_interval_min` (default 45; 15/30/45/60/90 in the UI) and
each tick runs exactly ONE phase, round-robin: `study → create → engage →
learn` — with up to 90 s of jitter so ticks never land on a metronomic beat.

What autopilot **does** automatically:
- refreshes niche data and the idea bank (study)
- writes algorithm-fit drafts (create)
- pulls mentions and drafts replies (engage) — replies stay **drafts**
  unless you opt into `[agent] auto_approve_replies` (default **false**),
  and even then they only become *approved + scheduled*, never sent
- rebuilds voice/metrics and reflects into the brain (learn)

What autopilot **never** does:
- **publish**. There is no publish phase, and there never will be. Approved
  content still ships only through the human-gated publish loop.
- bypass safety: all X writes go through the daily caps (4 posts / 10
  replies) + 5–20 s human-like delays regardless of who initiated them.

Each tick is isolated — a failed phase logs, joins the last-5 error ring on
the Insights card, and the next tick continues. State
(`{enabled, last_tick, next_tick, ticks, errors, phase}`) lives in the DB,
so autopilot survives restarts. `POST /api/autopilot/tick` forces a tick for
tests/manual runs; the interval and enabled flag persist via settings.

## Project layout

```
openstanley/
  core/    config.py (toml + .env loader), db.py, safety.py
  x/       client.py — XDry / XCookie (twikit) / XApi (official v2) — media + quote support
  gen/     algorithm.py (X ranking engine), lang.py (AR/EN), style_scan.py,
           voice.py (bilingual rubrics), ideas/drafts/replies/strategy,
           tools.py (chat tool registry), chat.py (streaming agent), agent.py (loops)
  server/  __main__.py — FastAPI + SSE + static web/dist
web/               React + Vite + TS + Tailwind + shadcn-style UI (see docs/API_CONTRACT.md)
data/              config.toml, openstanley.db, media/
tests/             pytest suites — no network, no real X calls
docs/              SPEC.md, API_CONTRACT.md, references/
```

## Tests

```bash
.venv/Scripts/python -m pytest tests/ -q          # all suites
.venv/Scripts/python tests/test_smoke.py          # direct runs still work
.venv/Scripts/python tests/test_e2e_fake_llm.py
```

⚠️ The e2e test writes junk rows into the real DB — wipe with
`python -c "import sys;sys.path.insert(0,'.');from openstanley.core import db;[c.execute(f'DELETE FROM {t}') for t in ('drafts','ideas','engagements','posts','agent_log','voice_profile') for c in [db.connect()]]"`
or delete `data/openstanley.db*`.

## Status

v0.3 — verified end-to-end (dry-run + real GLM-5.3 streaming): scan →
bilingual voice → algorithm-scored drafts (image/quote/scheduled-reply) →
approval gate → publish. See `docs/SPEC.md` and `UPGRADE_BRIEF.md`.
