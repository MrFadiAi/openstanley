# FIX BRIEF — Bootstrap must not persist unvalidated/healed cookies

## Context (found during live verification of df6f72b)
`POST /api/accounts/bootstrap` with a FAKE bare token returned
`{'ok': True, 'action': 'reconnected', 'handle': 'orbexai', 'followers': 1397}` and
**stored the fake token in the DB** (account 1 cookies poisoned; manually repaired by
the operator afterwards).

Root cause: `XCookie.me()` (openstanley/x/client.py) is wrapped in `_auto_heal`
(openstanley/x/cookie_heal.py) — on auth failure it silently pulls real cookies from the
logged-in Brave browser via CDP :9222 and retries. Bootstrap calls `me()` to verify
identity, the heal fires INSIDE that call, `me()` succeeds on the healed session, and the
caller then persists the ORIGINAL (fake) input token as if validated. The user is told
"reconnected" and everything looks fine until the browser session dies — then the account
runs on a token that never worked.

## Fix requirements
1. **No healing during bootstrap validation.** Identity verification must run with a
   no-heal variant (e.g. `me(heal=False)` or a context/disabled flag through the client).
   The `_auto_heal` wrapper must respect it (skip heal, let the original auth error
   propagate).
2. **Only validated cookies get stored.** If `me()` fails (including auth failure) →
   HTTP 400 with a helpful message (invalid token — re-copy it), DB untouched.
3. **Never store different cookies than what was validated.** If healing is ever
   legitimately used in a connect flow, the healed cookies are what must be persisted,
   not the input. Preferred: no heal anywhere in bootstrap/connect at all.
4. Audit other endpoints that persist cookies (`POST /api/x/cookie-connect`, settings
   accounts card, any `/api/accounts/*` cookie setters) — same rule for all: validate
   no-heal first, then store.
5. Keep auto-heal working for normal runtime reads/loops (that behavior is good) —
   this brief only removes heal from the *validation* path.

## Tests (hermetic; fake the X client seam, no network)
- bootstrap with fake token → 400, DB cookies unchanged (assert before/after equal)
- bootstrap with fake token does NOT trigger heal (heal function monkeypatched to fail
  the test if called)
- cookie-connect + settings cookie endpoints: same two assertions
- bootstrap with valid token (client faked ok) → stores exactly the submitted cookies
- runtime paths (loops) still heal on failure (existing behavior test, adjust if needed)

## Deliverable
Code + tests, full suite green (341 baseline), tsc clean if frontend touched (unlikely).
Commit with clear message. Report which endpoints were audited.
