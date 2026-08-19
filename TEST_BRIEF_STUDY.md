# TEST BRIEF — /study Telegram command (v0.4.4.1)

`/study` was just added to `openstanley/integrations/telegram.py` (`_cmd_study`, routed
like other commands in `_handle_update`, advertised in HELP_TEXT). It runs the server's
own loop runner through import → study → scan → learn and replies with a summary.

Your job: **test it like a skeptical reviewer — find what's broken before the user does.**
You may FIX small defects you find (and add the test that proves the fix), but stay in scope.

## What to verify (in order)

### 1. Routing & surface
- `/study` parses to the right command (space, `@bot`, uppercase variants).
- HELP_TEXT lists it. Unknown command still falls through to chat.
- Non-allowed chat gets refused and NEVER triggers the chain (spy).

### 2. Unit: `_cmd_study` directly (hermetic)
Call `_cmd_study(cfg)` with the existing hermetic fixtures (dryrun X, fake LLM,
`OPENSTANLEY_TEST_DB`). Assert:
- all four loops ran, in order import → study → scan → learn;
- the reply contains the four summary lines (📥 import / 📚 study / 🔬 scan / 🧠 learn)
  with REAL numbers from the loop results, then the ✅ line;
- the @handle + follower count renders when `me` is present;
- a failing loop (make scan raise) surfaces the error in the reply instead of
  silently dropping the line — if the current code swallows it, that's a defect: fix it.

### 3. Handler-level: simulated Telegram update
Fake httpx at the seam (pattern already used in `tests/test_telegram.py`): push an
update `{"message": {"chat": {"id": <allowed>}, "text": "/study"}}`, assert one reply
was sent whose text starts with 📥 and contains ✅, and that the four loops ran.
Long runtime note: the chain is synchronous in the handler — confirm the poller
thread isn't blocked forever if the chain hangs (timeout or thread discipline);
if there's no bound, flag it and add a reasonable one.

### 4. Loop-runner reuse
`_cmd_study` imports `_run_loop` from `openstanley.server.__main__`. Verify importing
the server module from the telegram module causes no circular import, no side effects
(port bind, scheduler start) when imported in a test process. If `_run_loop` is doing
something un-importable in tests, that's a defect — fix by extracting or guarding.

### 5. Regression
Full hermetic suite green. If the /study tests need a live event loop pattern the
existing tests don't have, follow `tests/test_autopilot.py` conventions.

## Rules
- Hermetic only: NO real X, NO real Telegram, NO real LLM, NO network. Never touch
  `data/openstanley.db` — conftest's `OPENSTANLEY_TEST_DB` must hold.
- Don't run the real server on :7878 (it may be in use).
- Small fixes allowed; big redesigns are out of scope — report instead.
- Commit with a clear message; report: what you tested, what you found, what you fixed.
