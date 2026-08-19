# OpenStanley v0.4.0 — Voice Lock (persona consistency enforcement)

The brain learned the voice (9-key rubric, style scan) and drafts score ~77 on voice
fidelity. But 77 means 1-in-4 drafts still drift. The account is an AI-agent persona
with deliberate quirks (lowercase, "speek"-style misspellings, short choppy posts) —
a single polished/corporate-sounding reply breaks character visibly. Nothing ENFORCES
consistency: the voice score exists only in the harness, not in the live pipeline.

Close the loop — score every draft, fix or reject drift BEFORE it reaches approval:

## 1. `openstanley/gen/voice_lock.py`
- `check_draft(cfg, text, kind) -> VoiceCheck {score_0_100, violations[], passed, fixed_text?}`
- **Fast deterministic layer** (no LLM): regex/heuristic checks derived from the brain's
  style profile (voice.md keys): casing rules (e.g. lowercase-first), length bands per
  kind (post < 280 but persona band from scan stats), banned-pattern list (emojis the
  persona never uses, corporate phrases: "delve", "game-changer", "excited to share",
  "In today's fast-paced world", exclamation stacking, hashtag walls), misspelling
  density band (persona uses SOME — both zero and too-many are drift).
- **LLM layer** (cheap, cached rules): only when deterministic score is borderline
  (55-80) — a single focused call: "rewrite in voice" → `fixed_text`. Score fixed_text
  deterministically again; pass the better one.
- `passed = score >= threshold` (config `[agent] voice_lock_threshold`, default 75).
- Loads persona rules from `data/brain/voice.md` (parse the scan-derived keys; if
  missing, fall back to built-in neutral rules and log a warn).

## 2. Pipeline wiring (create + engage + mentions + chat candidates)
- Every draft passes `check_draft` after LLM generation, BEFORE storing:
  - pass → store with `meta.voice = {score, checked: true}`
  - fix-succeeded → store fixed_text with `meta.voice = {score, fixed: true}`
  - fail → do NOT store; log `voice_lock rejected draft (score X, reasons)`; the loop
    continues (a failed draft is better than an off-voice one).
- Chat post-candidates (Write page) show the voice chip in ApprovalCard metadata.

## 3. UI
- DraftCard/ApprovalCard: small voice chip (mic icon, score, green/amber/red by
  threshold; tooltip lists violations; "fixed by voice lock" note when fixed).
- Settings page: voice-lock section — threshold slider, on/off toggle, and a live
  "test a line" input that runs check_draft and shows the verdict. EN+AR.

## 4. Tests (hermetic)
- Deterministic scorer: each violation class fires + clean persona text passes;
  length bands per kind; misspelling band both directions; brain voice.md parsing
  (present/absent); borderline → LLM fix path (fake LLM, fixed wins when better);
  fail → not stored + logged (spy); wiring: create/engage/mentions drafts carry
  meta.voice; threshold from config. ~12 tests → aim ~179. No network.

Hard rules: hermetic; LLM only in the borderline path; ONE new module + minimal touches;
approval gate untouched (voice lock runs before, not instead of, human approval).
