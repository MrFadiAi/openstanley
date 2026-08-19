# UPGRADE BRIEF — MULTI-ACCOUNT (v0.5.0)

Full account-scoped architecture: several X accounts in one OpenStanley install, each
with its own brain, posts, drafts, voice, caps and autopilot. User switches the active
account in the UI (and TG). Existing @orbexai data migrates cleanly — nothing lost.

Execute phases IN ORDER. After each phase: full hermetic suite green + git commit.
If you run out of runway, finish the current phase cleanly, commit, and report exactly
what remains. Honesty over ambition.

## Phase 1 — Account registry + DB scoping (core)
- New table `accounts(id INTEGER PK, handle TEXT, created_at TEXT, status TEXT default 'active')`.
- Migration: existing rows in account-scoped tables get `account_id INTEGER NOT NULL DEFAULT 1`.
  Scoped tables: posts, drafts, ideas, engagements, seen_mentions, metric_snapshots,
  identity_snapshots, voice_profile, eval_runs, eval_results. App-level (NOT scoped):
  settings (except new active_account_id), chat_messages, agent_log, accounts.
- `db.py`: every query on scoped tables filters by account_id. Prefer an explicit
  `acct` param over globals; where a global is unavoidable (long-lived loops), a module
  `active_account()` helper reading the setting, with set_active_account(id).
- Settings: `active_account_id` (default 1). /api/accounts: GET list (with handle,
  follower snapshot, post count), POST create (handle → new account, brain seeded
  fresh), POST /api/accounts/{id}/activate, DELETE (archives to
  data/accounts/archive-<handle>-<date>/ then removes rows).
- Cookie storage moves INTO accounts: `accounts.cookies_json` (write-only via API,
  masked in GET, scrubbed from logs like the TG token). `.env` cookies remain a
  bootstrap fallback for account 1 only, but DB wins when present.
- Safety caps keyed per account (posts/replies per day per account_id).
- Migration test: old DB file → migrated in place → all rows account_id=1, suite green.

## Phase 2 — Per-account brain + data dirs
- Brain files move from data/brain/ to data/accounts/<id>/brain/ (voice.md, rules.md,
  journal.md, instructions.md, strategy.md). Digests: data/accounts/<id>/digests/.
  Migration: move existing files to data/accounts/1/.
- brain.py + digest + voice scan take the account id (explicit param). Reflect/journal
  only ever touch the active account's brain.
- Fresh account = empty brain with default seed instructions; NOTHING from other
  accounts leaks (this is the user's hard requirement).

## Phase 3 — Server, agent loops, autopilot
- Every loop (import/study/scan/create/engage/mentions/learn/metrics/digest) operates
  on the ACTIVE account; switching accounts mid-run is safe (loops read account at
  start, log which account they ran for).
- Autopilot runs per active account only (single active account at a time is fine for
  v0.5 — simultaneous multi-account autopilot is explicitly out of scope).
- x/client.py: cookies resolved per account id (accounts.cookies_json), not global env.
- Smoke probe + engage gate + mentions naturally scoped via DB/brain scoping — verify.

## Phase 4 — UI + Telegram
- Account switcher in the app header (dropdown: handle + followers, "Add account",
  "Disconnect"). All existing pages render active-account data (they already go
  through the API — verify each endpoint is scoped).
- Connect tab: paste cookies → creates/selects that account (bootstrap flow like the
  very first run: identity check via me() → handle stored).
- Settings: manage accounts list (add/activate/archive), per-account cookies field.
- TG: /status first line = active account; new /account command (list, /account 2 to
  switch, allowed-chats gate still applies). TG chat operates on active account.
- i18n EN+AR for all new strings.

## Rules
- Hermetic tests per phase (fake _api/httpx/LLM seams as established; XSTANLEY-style
  env now OPENSTANLEY_*; test DB per phase).
- No real network in tests. Approval gate untouched. Suite fully green each phase.
- PROGRESS.md line per phase. Commit per phase: "v0.5.0-pN: <what>".
- Final report: phases done, migration path verified, what (if anything) remains.
