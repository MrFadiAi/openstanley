# UPGRADE — Rejection Learning Loop, Instruction Memory, Chat Watchdog

Three gaps in the learning surface, one build:

1. **Rejections teach nothing.** The owner's reject tap is the strongest
   signal in the system ("this does not represent me") and today it just
   flips a status column. The brain only learns from what got published.
2. **Chat instructions evaporate.** "Never end posts with questions" said in
   chat lives only in chat history; the every-10th-message reflect("chat")
   may catch it, diluted among 25 messages of material.
3. **Chat failures are silent.** LLM outages surface as a one-off
   "(LLM error)" bubble, runaway candidate storms make 9 drafts, the TG
   poller can degrade quietly — nobody is told until the owner notices.

## 1. Rejection learning loop — `gen/rejection_learn.py`

- `record_rejection(draft_id, reason, via, acct)` stamps
  `meta.{rejected_reason, rejected_at, rejected_via}` and is called at EVERY
  rejection point: web `POST /api/drafts/{id}/reject`, TG `/reject` command,
  TG inline reject taps, and the nightly expiry sweep (reason=`expired`).
- `pending_owner_rejections()` — rejected drafts whose meta carries a real
  owner decision (`rejected_reason != "expired"`) and no `rejection_learned`
  flag. Expiry rejections are queue hygiene, not taste — they are stamped
  but never learned as rules.
- Learning reuses the brain's own `reflect()` machinery via a new MATERIALS
  entry `rejections`: material = unlearned owner-rejected drafts (with kind,
  source, topic context) contrasted against recently approved/published
  drafts, so the LLM extracts *patterns the owner rejects*, not one-offs.
  Rules land with source=`rejection`; a journal entry always records the
  pass. Every learned draft gets `meta.rejection_learned=true`.
- Triggers: async at >=3 pending owner rejections (mirrors
  `maybe_reflect_chat_async`, daemon thread, never blocks the reject tap) +
  a nightly pass inside the 04:17 job BEFORE the expiry sweep runs (learn
  from real decisions first, then stamp the stale ones as expired).
- Manual: `POST /api/loops/rejection-learn`.

## 2. Instruction memory — `gen/instructions.py` + brain hoist

- Standing directives are stored as brain rules with source=`directive`
  (single store: they show in the Brain inventory, retire via the existing
  rule retirement, and survive as first-class brain content).
- Capture at chat time, both surfaces (web `chat_reply`/`chat_reply_stream`
  and the TG chat path): a deterministic EN+AR regex gate (`always|never|stop|
  don't|no more|from now on|avoid|rule:` … `دايما|أبدا|لا تنشر|توقف|بلا`)
  screens the user message; only gate passes spend ONE small LLM call
  (temp 0, 200 tokens) that confirms + normalizes the message into a
  <=140-char imperative rule. Ordinary messages cost nothing.
- `add_directive` dedupes (token-overlap >= 0.6 vs an active directive →
  skip), journals the capture, returns the rule id. The chat appends a
  visible ack: "🧠 Noted as standing rule R14 — I'll follow it from now on."
- `brain_context()` hoists: directive-sourced rules render as their own
  `OWNER DIRECTIVES (absolute — the owner said these)` block AHEAD of the
  learned RULES block, with its own budget share, so a flood of learned
  rules can never crowd the owner's law out of the prompt.
- Explicit path: `remember_rule {text}` chat tool — the model can persist a
  rule on request ("remember that my audience is Saudi builders").

## 3. Chat watchdog — `system/watchdog.py`

In-process health monitor, DB-setting state, never raises into callers:

| Watch | Trip | Action |
|---|---|---|
| chat LLM | 3 consecutive failures | TG alert + degraded flag (resets on first success) |
| tools | >50% failures over last 20 calls (min 10) | TG alert (once per episode) |
| chat drafts | 6 chat-born drafts in 1h | alert + `allow_chat_draft()` returns False until the window drains — the runaway guard made general |
| TG handlers | 10 consecutive handler errors | TG alert |

- `note(event, ok, detail)` / `allow_chat_draft()` / `status()` API.
- `GET /api/watchdog` + a health line in TG `/status`.
- Alerts ride `telegram.notify_bg` inside try/except — a broken Telegram
  can never break the monitored code paths.

## Tests

`tests/test_rejection_learn.py`, `tests/test_instruction_memory.py`,
`tests/test_watchdog.py` — hermetic, every LLM seam patched per-test.
