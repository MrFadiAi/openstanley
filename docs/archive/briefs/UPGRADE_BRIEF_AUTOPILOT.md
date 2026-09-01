# OpenStanley v0.3.5 — Autopilot (the actual OpenStanley product)

OpenStanley's core promise: the agent runs itself on a schedule — import → study → create →
engage → learn — and the human only approves. Today every loop is manual (button per loop).
Build autopilot so the product actually IS OpenStanley.

## 1. Scheduler-driven autopilot (`openstanley/gen/autopilot.py`)
- `AutopilotState` in DB settings: `{enabled, last_tick, next_tick, ticks, errors:[last 5], phase}`.
- One APScheduler job every `interval_minutes` (config `[agent] autopilot_interval_min`, default 45).
  Each tick runs a phase (round-robin with jitter): study → create → engage(dry: builds drafts ONLY) → learn.
  **Publish stays manual-approval forever** — autopilot never publishes, never bypasses the gate.
- Engagement cadence: engage phase may leave replies as approved+scheduled ONLY if
  `[agent] auto_approve_replies` is true (default FALSE).
- Safety: per-tick try/except, error → log + append to errors ring, next tick continues.
- All X writes still go through safety caps + human delay. Reads throttled.
- Tick uses its OWN DB writer discipline: no long transactions; log line per phase.

## 2. API + UI
- `GET /api/autopilot` → state. `POST /api/autopilot` `{enabled, interval_min?}` → start/stop.
- `POST /api/autopilot/tick` → force a tick now (returns phase results; used by tests).
- Dashboard: new "Autopilot" section on **Insights** page (keep nav clean):
  - toggle (with confirm), interval selector (15/30/45/60/90m), phase indicator with
    last-tick age, ticks count, error ring (last 5), "Run tick now" button.
  - Beautifului components, EN+AR strings, polished per the app's existing style.

## 3. Tests (hermetic; no real X)
- tick round-robin order + jitter bounds; state transitions; error ring keeps last 5;
  engage-with-autoapprove-off leaves zero approved replies; publish never called
  (spy/mock); API start/stop/force-tick; interval persistence.

## 4. Docs
README: "Autopilot" section — what runs automatically, what never does (publish),
how caps interplay. PROGRESS.md: add one line when done.

Hard rules: hermetic tests only (OPENSTANLEY_X_MODE=dryrun, no network), keep 104/104 green
(add ~10), ONE focused module + minimal server/UI touches, no real X calls from tests.
