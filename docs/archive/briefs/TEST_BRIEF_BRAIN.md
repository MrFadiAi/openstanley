# OpenStanley — Brain + Real-X Integration Test (READ-ONLY)

Mission: test EVERYTHING related to the Brain end-to-end and ensure it works 100%.
Use the REAL X session for reads. **ABSOLUTE RULE: DO NOT POST ANYTHING — no tweets,
no replies, no likes, no follows, no DMs, no profile edits. Zero write actions on X.**
Testing phase = read/scan/study only. If a test would require posting, verify the
approval gate BLOCKS it instead (that's a passing test).

## The real X session
- `.env` contains OPENSTANLEY_X_COOKIES (auth_token [+ ct0 if present]).
- KNOWN ISSUE: twikit 2.3.3 (even git master) currently fails with
  "Couldn't get KEY_BYTE indices" — X rotated its JS bundles and twikit's
  x-client-transaction header generator can't parse them.
- YOUR JOB includes making real reads work: diagnose and fix. Options in order:
  1. Check twikit GitHub issues/PRs for the KEY_BYTE fix — if a newer commit
     exists than the installed one, install it.
  2. If no fix: implement a workaround in openstanley/x/client.py — e.g. generate
     x-client-transaction-id ourselves (the algorithm is in twikit's
     x_client_transaction package — the failure is in fetching/parsing the
     JS home page; try pinning known-good bundle hashes or a cached copy of
     the home page from data/ if fetch fails).
  3. Alternative read path: fall back to a plain requests/httpx session with
     the auth_token cookie + ct0 for simple read endpoints (graphQL endpoints
     X uses) — read-only, no posting possible.
  4. Last resort: report precisely what's broken upstream and test the brain
     pipeline with synthetic account data instead (clearly labeled).
- Verify the connected account identity (me()) — reads only.

## Brain tests (all must pass 100%)
1. **Structure**: data/brain/ exists with instructions/rules/strategies/files/photos/journal — seeded sensibly.
2. **CRUD via API**: GET/PUT every part; edits logged to journal as "user edited".
3. **Sanitization**: content containing .env-style secrets (OPENSTANLEY_LLM_API_KEY=...,
   auth_token values) must be REJECTED.
4. **brain_context()**: budget-capped, includes rules + strategies; injected into
   chat/drafts/replies/ideas prompts (verify via fake-LLM capture in tests AND a
   real chat call — inspect that the reply obeys a rule you add, e.g. add rule
   "always end replies with 🧠" then ask OpenStanley something and check compliance).
5. **reflect()** — the core self-improvement:
   - reflect("chat"): feed N chat messages → expect structured edits applied
     (new rule or journal entry), journal updated with WHY.
   - reflect("learn"): synthetic metrics (one post over-performs 5x, one flops)
     → strategy update referencing them.
   - reflect("scan"): after account scan → niche/persona files updated.
   - Verify each trigger's edits appear in the right brain files and in journal.md.
6. **Hooks fire**: after 10 chat messages reflect("chat") runs automatically (counter
   in settings) — verify counter + invocation.
7. **Photos**: upload → saved to photos/ + .md sidecar; GET serves it.
8. **A/B brain-lift**: run harness A/B — with-brain scores must differ from
   without-brain; report the delta.
9. **UI**: Brain tab loads, file browser works, edit + save round-trips, reflect
   button runs and diff-flashes, journal timeline renders, photos grid works,
   AR/EN + RTL both fine. Verify in real browser (Playwright MCP).
10. **Full suite green**: .venv/Scripts/python -m pytest tests/ -q — all green,
    plus any new tests you add for gaps found.

## Real-X read tests (after read path works)
- me() → correct account (report handle + follower count).
- Deep scan (style_scan) on the REAL account: pulls real posts (respect safety
  caps + rate limits — batch with delays), builds style_profile, writes
  files/niche-map.md + audience-personas.md, triggers reflect("scan") → journal.
- Chat knows the account: ask OpenStanley "what's my most viral post?" → check the
  answer matches real account data.
- Engage loop DRY: find niche-relevant mention targets — read-only, drafts only,
  NOTHING sent. Verify drafts created + approval gate holds.
- Report: what was read, what was learned, what the brain wrote.

## Output
Fix a detailed markdown report to docs/TEST_REPORT_BRAIN.md: every test above
with PASS/FAIL + evidence, what you fixed (esp. the X read path), brain-lift
deltas, and the brain files' state after real-scan reflection. Commit everything.

## Constraints
Windows/Git-Bash, .venv/Scripts/python, server on 7878 (restart as needed),
NEVER print cookie values or API keys into logs/reports (redact), no real X
writes of any kind, approval gate must never be bypassed. If X reads are
impossible after all attempts, document exactly why + the synthetic fallback.
