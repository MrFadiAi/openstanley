# OpenStanley v0.3.2 — The Brain (self-improving agent memory)

Add a **Brain** section: the agent's own structured memory, written BY the agent itself.
It learns from chat conversations and the connected X account — and its brain files
are injected into every generation prompt (this is what makes it genuinely improve
over time instead of starting fresh each session).

## Structure — data/brain/ (plain files, git-friendly, human-readable)
```
data/brain/
  instructions.md    — how OpenStanley operates: its own operating manual (persona,
                       workflow, priorities). Self-maintained.
  rules.md           — learned DO/DON'T rules: what the user corrected, what flopped,
                       voice constraints, topics to avoid, X-platform lessons
                       (each rule numbered, with source + date)
  strategies.md      — accumulated growth strategies: what's working (from learn loop
                       metrics), posting theses, experiment log with outcomes
  files/             — reference docs the agent writes itself: niche-map.md,
                       audience-personas.md, content-pillars.md, voice-cards.md,
                       best-times.md (from analytics), competitor-notes.md
  photos/            — media library: uploaded/studied images. Each image gets a
                       sidecar .md note (alt text, when used, performance when posted)
  journal.md         — append-only reflection log: dated entries, what it learned
                       from each chat/session/scan, decisions made
```

## Backend — openstanley/gen/brain.py
- `BRAIN_DIR = data/brain/`; ensure structure on startup (seed sensible defaults
  first run — write an initial instructions.md in OpenStanley's voice).
- `read(part)` / `write(part, content)` — all parts above; atomic writes; sanitize
  (no secrets: refuse content containing the .env key patterns).
- `brain_context()` — compact rendering of instructions + rules + top strategies +
  active pillar summaries for prompt injection (token-budgeted ~1500 chars).
- `reflect(trigger, payload)` — THE self-improvement step. LLM call that reviews:
  - trigger "chat": recent chat_messages since last reflection + any user corrections
  - trigger "learn": post performance metrics (what over/under-performed)
  - trigger "scan": style profile / account data changes
  The LLM proposes structured edits: {instructions_delta, new_rules[], retire_rule_ids[],
  strategy_updates[], journal_entry} → applied deterministically (append/patch).
  Every change logged to journal.md with WHY.
- Hooks: reflect("chat") after every 10th chat message (cheap counter in settings);
  reflect("learn") at end of existing learn loop; reflect("scan") after deep scan.
- Photos: POST /api/brain/photos (upload, save to photos/, generate .md sidecar with
  LLM alt-text description note from context — GLM has NO vision, so alt-text comes
  from user-provided caption or usage context only).

## Prompt integration (the whole point)
- gen/chat.py SYSTEM: prepend brain_context()
- gen/drafts.py + replies.py + ideas.py: prepend brain_context() (rules + strategies
  directly shape generation — e.g. "rule 7: never use hashtags" actually applies)
- gen/strategy.py one-pager: read files/content-pillars.md + audience-personas.md
  as input, write the refined versions back after generation

## API
- GET  /api/brain              → {parts: [{name, type, size, modified, summary}]}
- GET  /api/brain/{part}       → file content (md or photo list)
- PUT  /api/brain/{part}       → user manual edit (also logged to journal: "user edited")
- POST /api/brain/reflect      → manual trigger {trigger: chat|learn|scan}
- GET  /api/brain/journal      → parsed journal entries
- POST /api/brain/photos       → upload
- GET  /api/brain/photos/{f}   → serve image
- GET  /api/loops/status (may exist from previous task — reuse)

## UI — Brain tab (beautifului primitives available in web/src/components/bui/)
- New "🧠 Brain" nav item (AR: الدماغ) between Strategy and Insights
- File-browser layout: left = file tree (instructions/rules/strategies/files/photos/journal
  with icons + modified dates), right = markdown viewer/editor with edit toggle
- Journal view: timeline of dated reflection entries (use bui StreamingText styling
  or simple cards) showing what changed and why
- "Reflect now" button (dropdown: from chat / from metrics / from scan) with
  LoadingState while the LLM reflects, then diff-flash the changed files
- Photos grid with captions; upload button
- Rules rendered as numbered list with source badges (chat/learn/scan + date);
  retired rules shown struck-through with "retired" state
- Manual edits via PUT (user is always allowed to edit the brain by hand)

## Tests
- brain structure seeding, read/write roundtrip, sanitization (secret-like strings rejected)
- reflect() with fake LLM: proposed edits applied (rule added, journal entry written)
- brain_context() budget cap + contains injected rules
- prompt integration: chat/drafts receive brain context (assert in fake-LLM capture)
- API endpoints CRUD + photo upload/serve
- ALL existing tests stay green: .venv/Scripts/python -m pytest tests/ -q
- NO real X calls, NO posting, approval gate untouched

## Constraints
Same as before: Windows/Git-Bash, .venv/Scripts/python, npm in web/, Tailwind v4
(now migrated), keep AR/EN + RTL working (Brain = الدماغ, add all new strings to i18n),
verify in real browser (Playwright MCP), build web/, restart on 7878, curl health,
commit at milestones, print summary.

## Win condition
OpenStanley gets smarter every week on its own: chats and scans produce new numbered
rules and strategy updates that measurably alter future drafts — all visible,
editable and auditable in the Brain tab.
