# OpenStanley v0.3.3 — Agent Harness (eval + quality measurement)

Build the **harness**: an evaluation + quality-measurement system that continuously
tests how good the agent actually is — voice fidelity, algorithm fitness, bilingual
quality, tool correctness — with tracked history so prompt/brain changes can be
regression-tested. (The Brain task may have just landed: data/brain/ exists —
the harness measures with and without brain context to prove self-improvement.)

## Backend — openstanley/harness/ (new package)
- `runner.py` — runs an eval suite against configurable LLM (fake in tests, real
  GLM in prod mode) and stores results in SQLite (new tables: eval_runs,
  eval_results — add to core/db.py with migration-safe CREATE IF NOT EXISTS).
- `suites/voice_eval.py` — N sample drafts generated from the voice profile;
  each scored for voice-match (style_profile distance: vocab overlap, sentence
  length distribution, punctuation habits, emoji density) → 0-100.
- `suites/algorithm_eval.py` — M generated drafts scored by gen/algorithm.py;
  metrics: mean score, % ≥65 (strong), % <35 (weak), factor distribution.
- `suites/bilingual_eval.py` — request posts in AR / EN / mixed; verify language
  correctness (lang.py detection), numeral conventions, RTL punctuation sanity.
- `suites/tool_eval.py` — scripted chat prompts ("schedule this for 9pm", "quote
  @naval", "best post this week") → assert correct tool + parsed args (fake LLM
  returns canned tool calls; real mode optional).
- `suites/safety_eval.py` — approval-gate attack tests: loop-generated drafts
  must NEVER move to published without approval; safety caps enforce; dry-run
  mode never touches network (mock client).
- `report.py` — aggregate a run: per-suite score 0-100, weighted total, deltas vs
  previous run, markdown report stored in data/harness/ + brain journal note if
  a suite regresses >10 points (feeds self-improvement).

## API
- POST /api/harness/run          → {run_id} then stream progress via SSE
  /api/harness/run/{id}/events (suites completing, live scores)
- GET  /api/harness/runs         → history list (date, total, per-suite, delta)
- GET  /api/harness/runs/{id}    → full detail + markdown report
- POST /api/harness/compare      → {a, b} diff two runs per-suite
- Config in data/config.toml [harness]: sample_count, suites enabled, real_llm bool

## UI — Harness tab (beautifului available: TaskRows, InsightCards-style cards, ScoreBadge)
- New "🧪 Harness" nav (AR: المِحراك) after Insights
- Run page: suite toggles + Run button → TaskRows live progress → per-suite score
  cards with deltas vs last run (▲▼ colored), weighted total ring
- History: runs table (date, total, Δ), click → detail report rendered markdown
- Compare view: two runs side-by-side per suite
- A/B mode: run suite twice — with brain context vs without — show the delta
  ("brain lift: +7.2 voice, +4.1 algorithm") — this is the self-improvement proof

## CLI
- `python -m openstanley.harness run --suites all --real` for terminal use,
  `python -m openstanley.harness report` prints last run summary.

## Tests
- Every suite runs against fake LLM deterministically → expected score ranges
- run → results persisted; second run produces deltas; regression detection fires
  journal note when engineered to regress (fake sequence)
- SSE events shape; API contract; safety_eval must catch an injected
  gate-bypass attempt (assert it fails closed)
- ALL existing tests stay green: .venv/Scripts/python -m pytest tests/ -q

## Constraints
Windows/Git-Bash, .venv/Scripts/python, npm in web/, Tailwind v4, AR/EN + RTL
(Harness = المِحراك in i18n), NO real X calls, NO posting, no secrets in results,
verify in real browser (Playwright MCP), build, restart 7878, curl health,
commit milestones, print summary.

## Win condition
One click shows: "Agent quality: 82/100 (▲3 vs last week) — voice 91, algorithm 74,
bilingual 88, tools 95, safety 100" and brain-lift deltas proving the agent
improves itself.
