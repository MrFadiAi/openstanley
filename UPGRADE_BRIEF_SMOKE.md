# OpenStanley v0.3.7 — Real-Data Smoke Harness (self-check on every start)

The app now touches the real account. But nothing verifies the LIVE wiring end-to-end:
tests are hermetic (fake X), and the dashboard only shows stale `last_scan` info.
When cookies silently expire at 2am, autopilot keeps ticking into a dead connection and
the user finds out days later. The harness evaluates fake scenarios — it can't catch this.

Build a "self-check" that runs REAL, cheap, read-only probes and reports honestly:

## 1. `openstanley/system/smoke.py` — live smoke probes (reads ONLY)
- `run_smoke(cfg) -> SmokeReport` runs, each with independent timeout + try/except:
  a. `identity` — `x.me()` → username/followers (confirms cookies + auth)
  b. `timeline_read` — `x.user_tweets(me.username, limit=5)` → count
  c. `search_read` — `x.search(agent.evergreen_themes[0], limit=5)` → count
  d. `notifications_read` — `x.notifications(limit=5)` (wrap: notif API can be flaky;
     failure = WARN not FAIL)
  e. `llm` — tiny gen.chat round-trip ("ping" → non-empty reply, max 30s)
  f. `brain` — `brain.context()` non-empty + latest journal entry readable
  g. `db` — quick write/read/delete on a `_smoke` settings key
- Each probe: `{name, ok, ms, detail}`. Overall: ok if a+b+c+e+f+g pass (d = warn-only).
- **Budget**: total ≤ 8 X-reads (read throttle respected), whole run ≤ 60s.
- NEVER posts. NEVER writes to X. Read-only by construction.

## 2. Server integration
- Runs automatically **once on startup** (async, non-blocking, after scheduler init),
  stores last report in settings `smoke_last`, logs a `system` line.
- `GET /api/system/smoke` → last report. `POST /api/system/smoke` → run fresh
  (rate-limited to 1/5min via settings timestamp).
- If `identity` fails: server log line warns "live X wiring broken — check Connect tab".

## 3. UI — Connect tab "System health" card
- Traffic-light summary (green/amber/red) with per-probe rows: name, ms, ok/warn/fail,
  detail. "Run self-check" button (disabled while running, spinner, 5-min cooldown).
- EN+AR strings, beautifului styling consistent with app.

## 4. Tests (hermetic)
- Fake X client (reads return fixtures; one variant raises 401) + fake LLM: all-pass
  report shape; identity-fail marks overall red; notifications-fail = amber only;
  POST-rate-limit on endpoint; startup integration doesn't block server boot
  (run in task, assert server serves / during smoke). ~10 tests → aim ~136 total.

Hard rules: reads only, no X writes, hermetic tests, ONE new module + minimal touches,
keep the suite green, no real network in tests.
