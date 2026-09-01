# OpenStanley v0.6 — Media Round-Trip (TG + Write chat)

The media pipeline already exists end-to-end — `data/media/` storage, `/api/media`
upload/serve, `drafts.image` column, Inbox compose attach, DraftCard thumbnails,
publish-with-media in all three X modes, +8 "Media boost" in the Algorithm Score.
Two surfaces never got it: **Telegram** (zero image support) and the **Write chat**
composer. v0.6 finishes the story. No schema changes, no new subsystems.

## 1. TG outgoing — approval cards show their image (`integrations/telegram.py`)

- `notify_new_drafts()` keeps the text card, then for every draft WITH an image
  sends `sendPhoto` (photo = `/api/media` bytes read from `data/media/<name>`),
  caption: `draft #<id> — <first ~200 chars of text> · reply /approve <id>`.
- GIFs (`.gif`) go as `sendDocument` — TG does not render them via sendPhoto.
- `sendPhoto` failure → fallback text line "(image attached — view it in Inbox)",
  mirroring the existing sendMessage HTML-entities fallback. Never breaks the card.
- Card send records `message_id → draft_ids` in a small in-memory dict
  (`_card_map: dict[int, list[int]]`) for reply targeting (see §2).

## 2. TG incoming — attach by reply or by caption

- `_handle_update` currently early-returns when a message has no text. Extend:
  if `msg.photo` (array) is present —
  a. **Reply path**: `reply_to_message.message_id` found in `_card_map` and maps
     to exactly ONE draft → attach to it. Ambiguous (multi-draft card) or missing
     → reply asking for the caption form.
  b. **Caption path**: caption parses as `/img <draft_id>` → attach to that draft
     (stateless, works forever, added to `/help`).
- Attach = download largest photo size (`getFile` → `file_path` → GET
  `https://api.telegram.org/file/bot<token>/<path>`), save to `data/media/` with
  the standard `media_<ts>_<hex>.jpg` naming + 5MB cap, `db.update_draft(image=…)`,
  send confirmation. Record in agent log.
- Videos / non-photo documents: polite decline message (v0.6 scope = photos only).
- Auth: photos pass through the same allowed-chats gate as text.

## 3. Write chat attach (`web/src/pages/Write.tsx`)

- Paperclip button in the chat composer → existing `uploadMedia()` (api.ts) →
  image chip with preview + remove — same pattern as Inbox compose.
- Staged image is included in the body when hitting **Save as draft** on a chat
  candidate (`POST /api/drafts` model already carries `image`).
- i18n EN+AR strings for the new composer controls.

## 4. Error handling

- Photo download failure / expired TG file → "couldn't fetch the photo, try again".
- Draft missing or already published → clear message naming the id.
- Photo > 5MB → explain the cap. Voice lock and Algorithm Score untouched.

## 5. Testing (hermetic, faked httpx per test_telegram.py pattern)

- Inbound photo (reply path, caption path, ambiguous card, non-allowed chat,
  oversized file) → file written to sandboxed MEDIA_DIR, draft updated, right reply.
- Card with image → sendPhoto payload captured; failure → fallback text.
- GIF draft → sendDocument. `/help` mentions `/img`.
- `POST /api/drafts` with image (extend existing coverage if absent).
- Web: build passes; composer chip behaves like Inbox's.

## Out of scope (later)

- Image generation, media library/gallery page, video attach, chat tool for
  image verbs, per-account media scoping (media is content, not persona state).
