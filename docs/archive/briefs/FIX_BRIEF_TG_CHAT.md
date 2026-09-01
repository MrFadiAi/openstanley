# FIX BRIEF — Telegram chat follow-up (3 defects found from live logs)

User tested the TG agent live (chat 5650490, 14:24–14:26). Conversation:
`'Hey' → 'Show me the drafts' → 'Can u draft a post for me' → 'Publish it now' → 'Yes'`
The agent replied per-message but did not FOLLOW UP — context and actions broke.
Root causes found in code review (verify, then fix):

## Defect 1 — chat-written posts are never saved (CRITICAL)
`openstanley/gen/chat.py` `chat_reply_stream` yields `{"type":"approval","candidate":…}`
events; the web UI saves those via `draft_from_chat`. The TG path
(`chat_reply_tg_stream` in `openstanley/integrations/telegram.py`) ignores
candidates completely → user drafts a post in TG, nothing is stored,
"/approve" and "Publish it now" then have nothing to act on.

FIX: after the stream finishes, run `chat_mod._extract_candidates(clean, cfg)`;
for each candidate call `chat_mod.draft_from_chat(cfg, candidate_text)` (or the
existing approveable-draft path used by the web UI) and append a line to the
final reply: `📝 saved as draft #<id> — /approve <id> to publish`. Keep the
approval gate sacred: saving a draft is fine, publishing is not.

## Defect 2 — poller processes updates strictly sequentially
`_poll_loop` does `await asyncio.to_thread(handle_update, cfg, upd)` one at a
time. A 30–60s LLM reply queues every later message behind it → the bot looks
dead when the user sends several messages quickly.

FIX: process each update in its own worker (thread pool or per-update task),
BUT preserve per-chat ordering (a small per-chat queue or lock) so replies to
the same chat don't interleave out of order. Different chats must run in
parallel. Bound the pool (e.g. 4 concurrent handlers) so 100 messages can't
spawn 100 LLM calls.

## Defect 3 — TG sessions are RAM-only; web and TG don't share memory
`_sessions` dict dies on restart; `chat_messages` DB (web) and TG memory are
separate worlds. `chat_reply_tg_stream`/`_remember` should persist each TG
turn to `chat_messages` (meta_json: `{"chat_id": <id>}`) and
`_history_turn` should rebuild from the DB when the RAM session is empty
(cap: last SESSION_CAP messages for that chat_id). Web UI chat stays chat_id 0
or absent — do not mix histories.

## Also verify (fix only if broken)
- "Show me the drafts" (plain text, not /drafts) — the LLM should have a tool
  to list pending drafts; if the tool doesn't exist, add `list_drafts` to the
  TG-visible tool set so natural language works like on the web.
- Typing indicator during long tool runs (post-stream) — nice-to-have, skip if risky.

## Rules
- Hermetic tests for all three fixes (fake _api seam, fake LLM stream, test DB).
- No real network in tests. Real DB untouched. Suite must be fully green.
- Keep streaming behavior (send_stream) as shipped — don't regress cc27f8f.
- Commit with a clear message; report what you changed.
