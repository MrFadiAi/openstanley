# FIX BRIEF — Telegram agent styling & Hermes-grade message quality

User feedback (verbatim intent): "the styling of writing and the text is very bad" and
"I want the telegram integration to be the same telegram integration as Hermes agent —
same structure, same idea."

Two problems found in review:

## Problem 1 — TG chat replies use the X-post voice (WRONG)
`chat_reply_tg_stream` builds its system prompt with `chat_mod._system(cfg, msg)` — the
DASHBOARD WRITE-ASSISTANT prompt. For the plain-chat path this leaks post-style quirks
into conversational replies: lowercase prose, X-voice punctuation habits, even typo
imitation ("beeing told wrong"). Live evidence in `chat_messages` (chat_id 5650490).

The X voice is for POSTS. The TG conversation is with the agent-as-assistant.

FIX — separate personas explicitly:
- Add a TG/assistant system prompt (new `_system_tg(cfg, msg)` or a mode flag on
  `_system`) that keeps all the brain context (voice rubric, rules, metrics, drafts,
  goals) but instructs: conversational replies are written as a clean, warm, direct
  assistant — proper casing, punctuation, concise; markdown allowed. The lowercase
  X-voice applies ONLY inside post drafts (the quoted candidate text), never to the
  surrounding conversation.
- Post candidates rendered inside replies keep the X voice verbatim (quoted block),
  untouched by voice_lock's chat-surface rules.
- Commands (/status, /drafts, /digest, /study replies) — audit each formatter for the
  same issue; they should be clean, consistently formatted, emoji-light, no lowercase
  gimmicks. One consistent template style across all commands.

## Problem 2 — zero Telegram formatting (no parse_mode)
`_api sendMessage/editMessageText` calls send plain text — no bold, no code, no links.

FIX — Hermes-style rich messages:
- Send with `parse_mode="HTML"` (safer than MarkdownV2 — no escape-everything traps).
- Add a tiny markdown→TG-HTML converter for LLM output: **bold**, *italic*, `code`,
  ```blocks``` (strip fences or map to <pre>), bullet lists, links. Escape stray
  & < > BEFORE inserting tags. Unbalanced markers → leave as literal text (never
  send a message Telegram would reject).
- editMessageText must use the same parse_mode (streaming edits + final edit).
- If Telegram returns 400 "can't parse entities": retry once with parse_mode removed
  (plain text) so delivery never fails on formatting.
- send_stream's progressive edits already throttle — keep that; only add formatting.
- Keep messages under 4096 chars (MSG_LIMIT clip already exists — apply BEFORE
  tag insertion so tags aren't cut mid-way).

## Reference: how Hermes does it (structure to mirror)
- Streaming assistant replies via progressively-edited message bubbles (we have this).
- Rich but restrained formatting: short bold labels, bullet lists, code for ids/
  commands, links as links. Not walls of emoji.
- Assistant speaks as itself in normal prose; quoted/generated content (drafts) keeps
  its own style inside quote blocks.
- Media/files when relevant — SKIP for now (out of scope).

## Rules
- Hermetic tests: converter unit tests (bold/code/lists/escape/unbalanced/clip),
  parse_mode present on sendMessage + editMessageText, 400→plain-text retry, TG
  persona prompt actually used by chat path, command formatters clean.
- No real network in tests (fake _api seam as in tests/test_tg_stream.py).
- Don't regress: streaming (cc27f8f), follow-through drafts (c5ed3bf), 260 tests green.
- Run full suite, commit, report.
