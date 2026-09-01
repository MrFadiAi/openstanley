# FIX BRIEF — Bare-token cookie paste (no JSON required)

User (verbatim intent): "I'm trying to connect my Twitter cookies with the app, but it
asks me to paste a JSON auth token. I don't want to paste the JSON. I just have the
auth token. I want like when I paste it to be automatically there as JSON. I don't
have to do the JSON structure by myself."

## Goal
Every cookie-input surface accepts a **bare auth_token string** and auto-wraps it into
the JSON structure internally. The user never writes JSON.

## Input forms that must ALL work (server-side, one normalizer)
1. `a1b2c3d4e5f65ca...` (bare token, 40+ hex chars) → `{"auth_token": "<token>"}`
2. `auth_token=db22...; ct0=abc123...` (browser cookie-header format, any separator:
   `;`, newline, whitespace) → `{"auth_token": "...", "ct0": "..."}`
3. Existing full JSON `{"auth_token": "...", "ct0": "..."}` → unchanged behavior
Trailing whitespace/quotes around the pasted value must be tolerated (strip `"` `'`).

## Surfaces to wire (same normalizer everywhere)
- `POST /api/accounts/bootstrap` (Connect wizard, server/__main__.py ~line 1514)
- `POST /api/accounts/{id}/cookies` (~line 1456)
- account-create path (~line 1420, `cookies_json: str | None` field)
- legacy `/api/connect` endpoint if it still exists (check)
- `.env` `OPENSTANLEY_X_COOKIES` via `resolve_cookies` (client.py ~line 540 — already
  half-tolerant: extend the try to use the normalizer, not just accept dicts)

## Where the normalizer lives
`openstanley/x/client.py` — `normalize_cookies_input(raw: str) -> str | None`:
returns the canonical JSON string, or None if no auth_token can be extracted
(raise nothing; endpoints translate None into the existing 400 with a HELPFUL message:
"Paste your auth_token (the long token from the x.com 'auth_token' cookie), or the full
JSON — either works").

## Frontend (Connect.tsx + Settings account cookies field)
- Placeholder + helper text change: "Paste your auth_token (or full cookies JSON) —
  anything works"
- i18n EN+AR for new strings (AR: "الصق توكن auth_token (أو JSON كامل) — أي شيء يعمل")
- NO client-side JSON building — the server normalizes; the frontend just sends raw text.

## Tests (hermetic)
- Normalizer unit tests: bare hex token / cookie-header both separators / JSON pass-through /
  quoted values / whitespace / garbage → None.
- Endpoint tests: bootstrap + set-cookies accept bare token and store canonical JSON
  (masked hint reflects `auth_token` present); 400 message helpful on garbage.
- Full suite green (baseline 310 + polish build may have added more — run and match).

## Constraints
- Do NOT weaken validation: a value with no extractable auth_token is still a 400.
- Tokens remain write-only/masked/never-logged (existing convention).
- Frontend build must stay clean (tsc).

## Deliverable
Commit + report. Include one example request per input form.
