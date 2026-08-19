# OpenStanley v0.3.4 — Cookie Auto-Heal (durable real-account connection)

Problem: X rotates cookies (ct0 ~daily, auth_token on any browser re-login). The app
now runs in cookie mode against @orbexai; when cookies expire every read/write dies
until a human re-pulls cookies. `scripts/pull_x_cookies_cdp.py` already proves the
fix works: pull fresh cookies from live Brave via CDP port 9222.

Build `openstanley/x/cookie_heal.py` + integration:

1. **Detector**: wrap XCookie request path — on twikit auth failures (401 code 32,
   403 code 353, or KEY_BYTE-family exceptions) mark cookies stale ONCE (cooldown
   10 min between heal attempts; never loop).
2. **Healer** `async heal_cookies() -> bool`:
   a. If Brave already listens on 127.0.0.1:9222 → reuse (CDP `Network.getAllCookies`).
   b. Else: no auto-launch of browsers (surprising) — heal fails gracefully.
      (Manual path documented in UI instead.)
   c. Pull x.com auth_token+ct0; if auth_token CHANGED, persist to `.env`
      (`OPENSTANLEY_X_COOKIES=` compact JSON, preserve all other lines, atomic write),
      rebuild the twikit client, reset `me` cache, log `system` entry.
   d. Same auth_token but new ct0 → update env value in-memory + .env, rebuild.
3. **Status**: `/api/x/status` gains `cookies_stale: bool`, `last_heal: iso|None`,
   `heal_ok: bool|null`. Frontend Connect tab shows a "Session auto-healed ✓" /
   "Cookies expired — restart Brave with --remote-debugging-port=9222" banner state.
4. **Tests** (hermetic, no real X): unit-test detector classification (fake exceptions
   → stale/not-stale), .env atomic rewrite preserving foreign lines, cooldown logic,
   rebuild trigger. Mock the CDP pull function. No network in tests.
5. README: short "durable cookies" section — keep Brave open with the debug flag;
   openstanley self-heals forever.

Hard rules: never POST to X in tests; no real network calls in the test suite;
one focused module + minimal touches elsewhere; keep 91/91 green (add tests to reach ~96+).
