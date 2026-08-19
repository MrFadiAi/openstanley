# Brain + Real-X Integration Test Report

**Date:** 2026-08-19 · **Suite:** `tests/` → **91 passed** (78 baseline + 13 new) · **X writes: 0 (verified)**

Mission: test everything Brain-related end-to-end; fix the twikit KEY_BYTE
read path; use the real X session for reads only.

---

## ⚠️ Environment blocker found first (read before the scores)

`TEST_BRIEF_BRAIN.md` states *".env contains OPENSTANLEY_X_COOKIES (auth_token
[+ ct0 if present])"*. **It does not.** Verified exhaustively:

| Where cookies would live | Check performed | Result |
|---|---|---|
| `.env` | enumerated every line, key names + value lengths only | only `OPENSTANLEY_LLM_API_KEY` (1 line) |
| process env | `env \| grep OPENSTANLEY` | nothing |
| `data/config.toml` | parsed, printed keys with values redacted | no cookies key |
| `data/openstanley.db` `settings` | all setting keys listed | `me` = dry-run local user |
| repo-wide `auth_token` grep | toml/env/json/txt/cfg | no hits outside code/docs |

Consequence: **authenticated real-X reads (me(), deep scan of the real
account) could not be executed — no credentials exist in this environment.**
Everything else ran for real. The read path itself was fixed and verified
against the live x.com home page anonymously (see §1). Paste cookies via
Dashboard → X-Connect (or `.env` → `OPENSTANLEY_X_COOKIES={"auth_token":"…","ct0":"…"}`)
and the real-account scan runs exactly as specced — no code changes needed.

---

## 1. The twikit KEY_BYTE read-path fix — FIXED (verified against live x.com)

### Diagnosis (evidence, not guesswork)

- Installed: twikit **2.3.3** (PyPI latest; `requirements.txt` pins it).
  Upstream `d60/twikit` master has had **no code change since 2025-04** —
  no merged fix exists.
- The community fix, **PR d60/twikit#432** ("Fix KEY_BYTE indices parsing…",
  still open, adapted from #411), replaces the `ondemand.s` chunk-lookup
  regexes with a two-step chunk-index → hash lookup.
- **My own anonymous probe of `https://x.com` (2026-08-19)** shows the
  situation is worse than PR #432 assumed: the logged-out home page now
  serves the new **`x-web` bundle** (`entry-client-logged-out-CtmNJRs8.js`)
  with **zero `ondemand` references** — *neither* twikit 2.3.3's regex *nor*
  PR #432's regex matches it. (The page still serves the
  `twitter-site-verification` key and all four `loading-x-anim` frames.)
  Cookie-authenticated pages may still serve the legacy manifest — that is
  exactly what the patch handles both ways.

### The fix — `openstanley/x/twikit_patch.py` (new, runtime, idempotent)

Applied by `XCookie._ensure()` before any twikit call; survives venv
reinstalls; skips cleanly if a future twikit restructures.

| # | Patch | Why |
|---|---|---|
| 1 | `ClientTransaction.get_indices` → two-step chunk lookup (PR #432 logic) **+ legacy-format fallback** | finds the manifest in both webpack generations |
| 2 | **Graceful degradation**: if no manifest is found, mark degraded and let requests proceed **without** `X-Client-Transaction-Id` instead of raising "Couldn't get KEY_BYTE indices" (vendored, marker-checked copy of twikit 2.3.3's `Client.request`; the header is a bot-scoring signal, not a hard gate — never send a bogus one) | the actual crash every request died on |
| 3 | `User.__init__` guards (auth + guest): `entities.description.urls`, `pinned_tweet_ids_str`, `withheld_in_countries` now default instead of `KeyError` | PR #432's second half; X omits these fields on many accounts |

### Verification

- **Live x.com (anonymous, no cookies):** patched `ClientTransaction.init()`
  completes — `init OK — no KEY_BYTE exception`, `degraded: True` (correct:
  today's logged-out page has no manifest), header correctly **omitted**.
  Before the patch this exact call raised
  `Exception: Couldn't get KEY_BYTE indices`.
- **Offline unit tests** (`tests/test_x_readpath.py`, 8 tests): both manifest
  formats parsed, degraded path never raises, sparse user payloads survive,
  vendored `request` omits the header when degraded / sets it when computable,
  `XCookie._ensure` wires the patch in, patch application is idempotent.
- **Not verifiable without cookies:** the authenticated GraphQL round-trip.
  Documented as the one open item; first real `me()` call will prove or
  refute X's tolerance of headerless reads (fallback keeps twikit's entire
  GQL layer intact, so if X rejects, the error will be a clean HTTP 4xx we
  can act on).

**PASS** (fix + offline + anonymous-live verification) · authenticated half **BLOCKED — no credentials in environment**

---

## 2. Brain test groups 1–10

### ① Structure — PASS
`data/brain/` = instructions/rules/strategies/journal + files/ (6 seed docs) + photos/.
Seeds are in OpenStanley's voice. Inventory API returns 11 parts with summaries
(`2 active rules (3 total)`, `26 entries`, `1 photos` — observed live in the UI).

### ② CRUD via API — PASS
GET/PUT round-trips on instructions/rules/strategies + all `files/*`. Manual
PUT journals `user-edit:<part>` ("user edited this file by hand" — seen 3× in
the live journal). Unknown part → 404; traversal (`files/../../etc/passwd`) →
FileNotFoundError. Covered by `test_api_brain_endpoints` + browser round-trip (§⑨).

### ③ Sanitization — PASS
Rejected (400 / BrainSecurityError, nothing stored): `OPENSTANLEY_LLM_API_KEY=…`,
`api_key: sk-…`, `auth_token: …` (32×a), `{"cookies": "<hex>"}`, `bearer eyJ…`
(JWT), `ct0=…`, `PASSWORD=…`. Normal text (incl. Arabic `؟ ، ؛`) accepted.
Reflect-proposed secrets are dropped and counted (`dropped_tainted`) —
`test_reflect_drops_tainted_proposals`.

### ④ brain_context() injection — PASS (both halves)
- Budget: ≤1500 chars (also enforced at budget=300); includes active rules
  (`R1:…`), strategies, pillars.
- Fake-LLM capture: chat + drafts prompts start with the BRAIN header
  (`test_chat_prompt_receives_brain`, `test_drafts_prompt_receives_brain`).
- **Real-LLM compliance:** added rule *"R99: DO always end chat replies with
  the 🧠 emoji"* → real glm-5.3 chat call via `/api/chat`. Attempt 1 missed
  (temperature 0.85 variance — the rule was verifiably IN the prompt);
  retry complied: reply ended `…Want me to draft it?\n\n🧠`. Rule then retired
  (shows struck-through in UI). A/B toggle verified: `set_brain_enabled(False)`
  → `brain_context() == ""`, restore → header back.

### ⑤ reflect() — PASS (chat, learn, scan — real LLM + fakes)
- **chat** (real LLM, UI-triggered): added R100 ("DON'T confirm an
  absolute-time schedule until tz is resolved"), instructions delta re R2
  violated in practice, strategy `quote-post / hot-take [working]` — all
  journaled with WHY.
- **learn** (synthetic 5x-over-performer vs flop, fake LLM): material carried
  the real OVER/UNDER-PERFORMED rows; strategy + rule applied.
- **scan** (dry data, **real LLM**): scanned 160 posts → added R101-R103
  (colon-led one-liner voice, no emoji/hashtags, self-deprecating hooks),
  strategy `Aphoristic one-liner voice [working]`.
- **NEW GAP FOUND → FIXED:** nothing wrote `files/niche-map.md` /
  `files/audience-personas.md` after scans (specced in the test brief, absent
  in the implementation). Added to `reflect()`:
  - LLM may propose `file_updates` (seed-file stems only — `journal`/path
    traversal attempts are ignored, tested);
  - deterministic stats-derived fallback guarantees both docs absorb every
    scan. Verified live: both files rewritten (`# Niche Map (scan 2026-08-19)`
  with real topics/hours/language-mix) + journaled `file niche-map: …`.

### ⑥ Hooks fire — PASS
Counter live in settings (`brain_chat_counter` = 16 during testing); every
10th message spawns reflect("chat") in a daemon thread
(`test_chat_reflect_hook_every_10th` + live journal shows `reflect:chat`
entries from the automatic path at msg 10).

### ⑦ Photos — PASS
Upload (via real browser file-picker) → `data/brain/photos/brain_test_photo_d4091ab1.png`
+ `.md` sidecar (caption, date, honest "no vision" note) → grid renders the
figure with caption → served byte-identical via `/api/brain/photos/<name>`.
Bad type → 400; traversal/missing → 404. UI count went `0 → 1 photos`.

### ⑧ A/B brain-lift — PASS (real run via API, run #628 vs #629)

| suite | no-brain | with-brain | lift |
|---|---|---|---|
| algorithm | 59.0 | 80.0 | **+21.0** |
| voice | 69.3 | 77.0 | **+7.7** |
| bilingual / tools / safety | 100 | 100 | 0.0 (brain-independent, correctly flat) |
| **total** | **80.5** | **88.1** | **+7.6** |

Signed into the brain journal as `harness:ab` entries ("meaningful brain
present"). Earlier same-day A/B before rules existed: 75.6 vs 75.6 (flat) —
the lift appears exactly when the brain learns its first rules.

### ⑨ Brain tab in real browser (Playwright) — PASS
Verified against the live server (127.0.0.1:7878):
- **AR + RTL** (default): full tab renders — nav `الدماغ`, file browser
  (Memory / ملفات مرجعية ×6 / السجل), `تأمل الآن`, edit flow, rules with
  status badges (`2 نشطة · 1 متقاعدة`, R99 shown struck-through as متقاعدة),
  journal timeline (26 dated entries with trigger chips + change lists),
  photos grid + caption dialog + honest no-vision note.
  Evidence: `docs/evidence/brain-tab-ar-rtl.png`
- **EN + LTR** (toggle in Settings): everything re-renders correctly.
  Evidence: `docs/evidence/brain-tab-en-ltr.png`
- **Edit + save round-trip:** edited rules.md raw text in the browser (added
  R3), saved → parsed list re-rendered with R3 + source badge + date; journal
  gained `user-edit:rules`.
- **Reflect button:** dropdown (من المحادثة/من القياسات/من الفحص) → real LLM
  reflection ran and its edits appeared (see ⑤ chat row).

### ⑩ Full suite green — PASS
`.venv/Scripts/python -m pytest tests/ -q` → **91 passed** (server stopped
during runs, per known-env-quirk). New: 8 read-path tests + 5 brain tests
(scan file_updates ×3, learn metrics, A/B toggle).

---

## 3. Real-X read tests

| Test | Result |
|---|---|
| `me()` on real account | **BLOCKED — no credentials** (see top). Read path itself fixed & anonymously verified (§1). |
| Deep scan (`style_scan`) on real account | **BLOCKED for real account** — pipeline itself proven on dry data with the real LLM (160 posts → style_profile → reflect:scan → R101-R103 + niche-map/audience-personas, §⑤). Safety caps + batching + 2-2.7s read-throttle in place (`_throttle_reads`). |
| Chat knows the account | **PASS (wiring)** — "what's my most viral post?" → ran `query_analytics`, answered with the true top post ("3 years of side projects…", real like/reply counts from the tool result). Dry-data numbers; identical code path serves the real account once cookies exist. |
| Engage loop DRY | **PASS** — 3 niche-reply drafts created ([1848]-[1850], status `draft`), 0 sent. |
| Approval gate holds | **PASS** — `publish` loop with 11 unapproved drafts → `published: []`, all 11 still pending. (7 older drafts pre-approved earlier in the day drained at server startup — every one logged `[dry-run] would post`, mode=dryrun, safety counters posts/replies = 0/0.) **Zero X writes of any kind, all session.** |

## 4. What the brain now knows (state after this session)

- **rules.md**: R1 (bilingual), R2 (timezone), R3 (concrete hooks — browser
  test), R100 (tz confirm before scheduling), R101-R103 (scan-derived voice:
  colon-led one-liners, no emoji/hashtags, self-deprecating contrarian hooks);
  R99 (🧠 probe) retired.
- **strategies.md**: Bilingual content experiment [new], Aphoristic
  one-liner voice [working], quote-post/hot-take [working].
- **files/**: niche-map.md + audience-personas.md now carry real scan-derived
  content (topics, hours, language mix) — previously eternal stubs.
- **journal.md**: 26+ entries — every user edit, reflect (chat/scan), harness
  A/B lift, each with WHY.
- **photos/**: 1 test photo + sidecar (will delete on request).

## 5. Files changed this session

| File | Change |
|---|---|
| `openstanley/x/twikit_patch.py` | **new** — the KEY_BYTE fix (3 layers) |
| `openstanley/x/client.py` | `XCookie._ensure` applies compat patches (logged) |
| `openstanley/gen/brain.py` | reflect(): `file_updates` (seed-files only) + deterministic scan fallback for niche-map/audience-personas; richer scan material (top own + niche posts); log line includes file count |
| `tests/test_x_readpath.py` | **new** — 8 tests |
| `tests/test_brain.py` | +5 tests (scan files, seed-file guard, learn metrics, A/B toggle) |
| `docs/evidence/*` | browser screenshots (AR/RTL + EN/LTR) + test photo |
| `.gitignore` | `data/server.log` |
| `data/brain/**`, `data/harness/**` | the brain's live state + run history (git-friendly by design) |

**Open items:** (1) real-account reads await `OPENSTANLEY_X_COOKIES` — first
`me()` + deep scan should be re-run the moment they land; (2) if X rejects
headerless GraphQL reads, next step is porting the x-web transaction
generator (upstream has none — `d60/twikit#408` tracks it).
