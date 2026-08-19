# Media Round-Trip (v0.6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Images work on the last two surfaces — Telegram approval cards show photos, replying with (or captioning `/img <id>`) attaches one, and the Write chat composer can stage an image onto a saved draft.

**Architecture:** Everything rides existing primitives: `data/media/` + `MEDIA_DIR`, `drafts.image` column, `POST /api/media` upload, `db.update_draft(image=…)`, the `attach_media` endpoint's validation rules. Telegram gets a multipart outbound path (`sendPhoto`/`sendDocument`), an inbound photo handler (caption command + reply-to-card map), and the Write page threads a staged image into `POST /api/chat/draft` (extended with `image`).

**Tech Stack:** Python 3.11 / FastAPI / httpx (sync, faked at the `tg.httpx` seam in tests) / React+TS (vite). Spec: `UPGRADE_BRIEF_MEDIA.md`.

**Working dir:** `D:\Ai\openstanley`. Test runner: `.venv\Scripts\python -m pytest`. Frontend build: `cd web && npm run build`.

---

### Task 1: TG outbound — approval cards send their image

**Files:**
- Modify: `openstanley/integrations/telegram.py` (outbound section, after `send_message` ~line 470; `notify_new_drafts` ~line 593)
- Test: `tests/test_telegram.py` (extend the `FakeTG` class + new tests at end of file)

- [ ] **Step 1: Extend the fake httpx to capture multipart sends**

In `tests/test_telegram.py`, the fake transport class (defined near line 80, the one with `self.calls` / `def post(self, url, json=None, timeout=None, **kw)`). Change its `post` to also accept multipart payloads and record them:

```python
    def post(self, url, json=None, timeout=None, files=None, data=None, **kw):  # noqa: A002
        method = url.rsplit("/", 1)[-1]
        params = dict(json or {}) if json else dict(data or {})
        if files:
            # multipart (sendPhoto/sendDocument): record the filename
            params["_file"] = list(files.values())[0][0]
        self.calls.append((url, method, params))
        if method == "getUpdates":
            if self.status != 200:  # e.g. 401 → the bad-token path
                return _R(self.status, {"ok": False})
            if self.batches:
                return _R(200, {"ok": True, "result": self.batches.pop(0)})
            if self.on_exhausted:
                self.on_exhausted()
            return _R(200, {"ok": True, "result": []})
        return _R(200, {"ok": True, "result": {"message_id": 4242}})
```

(If the class's `__init__` already matches, only `post` changes. Keep the existing `sent()` helper as-is.)

Add a helper next to `sent()`:

```python
    def media_sends(self) -> list[tuple[str, int, str, str]]:
        """(method, chat_id, caption, filename) of every multipart send."""
        return [(m, p.get("chat_id"), p.get("caption", ""), p.get("_file", ""))
                for _u, m, p in self.calls if m in ("sendPhoto", "sendDocument")]
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_telegram.py` (reuse the file's existing imports/fixtures — `_enable`, `CHAT`, `FakeTG`, `_push` if defined; check the file's existing helpers for driving updates through the poller; if tests there drive `_handle_update` directly, do the same):

```python
def _img_draft(text="media draft text") -> int:
    d = db.add_draft(text=text, acct=1)
    db.update_draft(d, image="media_test_photo.png", acct=1)
    return d


def test_card_with_image_sends_photo(tmp_path, monkeypatch):
    _enable()
    (tmp_path / "media_test_photo.png").write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = FakeTG()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._card_map.clear()
    r = tg.notify_new_drafts([_img_draft()])
    assert r["ok"]
    sends = fake.media_sends()
    assert len(sends) == 1
    method, chat, caption, fname = sends[0]
    assert method == "sendPhoto" and chat == CHAT
    assert fname == "media_test_photo.png"
    assert "#1" in caption  # draft id appears in the caption


def test_card_gif_sent_as_document(tmp_path, monkeypatch):
    _enable()
    (tmp_path / "media_test.gif").write_bytes(b"GIF fake")
    d = db.add_draft(text="gif draft", acct=1)
    db.update_draft(d, image="media_test.gif", acct=1)
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = FakeTG()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.notify_new_drafts([d])
    sends = fake.media_sends()
    assert sends and sends[0][0] == "sendDocument"


def test_sendphoto_failure_falls_back_to_text(tmp_path, monkeypatch):
    _enable()
    (tmp_path / "media_test_photo.png").write_bytes(b"x")

    class FailPhoto(FakeTG):
        def post(self, url, json=None, timeout=None, files=None, data=None, **kw):  # noqa: A002
            if url.endswith("/sendPhoto"):
                return _R(400, {"ok": False, "description": "Bad Request"})
            return super().post(url, json=json, timeout=timeout, files=files,
                                data=data, **kw)

    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = FailPhoto()
    monkeypatch.setattr(tg, "httpx", fake)
    r = tg.notify_new_drafts([_img_draft()])
    assert r["ok"]  # card still delivered
    texts = " ".join(t for _c, t in fake.sent())
    assert "image attached" in texts
```

- [ ] **Step 3: Run — expect failure**

Run: `.venv\Scripts\python -m pytest tests/test_telegram.py -q -k "card_with_image or gif_sent or sendphoto_failure"`
Expected: FAIL — `AttributeError: module 'openstanley.integrations.telegram' has no attribute 'MEDIA_DIR'` (or `_card_map`).

- [ ] **Step 4: Implement**

In `openstanley/integrations/telegram.py`:

(a) Near the top constants (after `MSG_LIMIT = 4000` line ~53):

```python
FILE_URL = "https://api.telegram.org/file/bot{token}/{path}"
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # matches the /api/media upload cap
```

(b) Imports section — extend the existing `from ..core.config import Config` (line 42) to also pull `ROOT`, then after the module-state block (~line 80):

```python
from pathlib import Path
MEDIA_DIR = Path(  # same folder /api/media writes to; monkeypatched in tests
    __import__("openstanley.core.config", fromlist=["ROOT"]).ROOT
) / "data" / "media"
```

(Cleaner: change line 42 to `from ..core.config import Config, ROOT` and write `MEDIA_DIR = ROOT / "data" / "media"` — do it that way.)

Also add the card map next to `_sessions` (~line 79):

```python
_card_map: dict[int, list[int]] = {}  # card message_id → previewed draft ids
```

(c) Outbound section, after `send_message` (~line 470):

```python
def send_photo(chat_id: int, image_name: str, caption: str = "") -> dict:
    """One outbound photo/document. Same contract as send_message: rate-
    limited, never raises. GIFs go as documents (TG won't render them as
    photos); anything else as sendPhoto. Falls back to a text line on
    failure so a media problem never costs the card."""
    token = bot_token()
    if not token:
        return {"ok": False, "status_code": None, "error": "no bot token"}
    path = MEDIA_DIR / image_name
    if not path.exists():
        return {"ok": False, "status_code": None, "error": "no such media file"}
    if not _rate_allow():
        db.log("telegram", f"rate limit hit — photo to chat {chat_id} dropped",
               level="warn")
        return {"ok": False, "status_code": None, "error": "rate limited"}
    method = "sendDocument" if image_name.lower().endswith(".gif") else "sendPhoto"
    try:
        r = httpx.post(API_URL.format(token=token, method=method),
                       files={(method == "sendPhoto" and "photo" or "document"):
                              (image_name, path.read_bytes())},
                       data={"chat_id": chat_id, "caption": _clip(caption)},
                       timeout=HTTP_TIMEOUT_S)
        ok = 200 <= r.status_code < 300
        if not ok:
            db.log("telegram", f"{method} to chat {chat_id} failed "
                               f"(HTTP {r.status_code})", level="warn")
        return {"ok": ok, "status_code": r.status_code, "error":
                None if ok else r.text[:200]}
    except Exception as e:  # noqa: BLE001
        db.log("telegram", f"{method} to chat {chat_id} error: "
                           f"{_scrub(str(e), token)}", level="warn")
        return {"ok": False, "status_code": None, "error": _scrub(str(e), token)[:200]}
```

(d) Rewrite `notify_new_drafts` (line 593) — after the text card, push each imaged draft and record the map:

```python
def notify_new_drafts(draft_ids: list[int]) -> dict:
    """Compact 'needs approval' card for drafts a loop just created.
    Drafts with an image get their photo pushed right after the text card,
    so the human SEES the visual before /approve."""
    if not draft_ids:
        return {"ok": False, "sent": 0, "chats": 0, "error": "no drafts"}
    rows = [d for d in (db.get_draft(i) for i in draft_ids) if d]
    if not rows:
        return {"ok": False, "sent": 0, "chats": 0, "error": "no drafts"}
    result = notify(drafts_card(rows))
    previewed = [d["id"] for d in rows[:DRAFTS_PAGE]]
    for chat in allowed_chats():
        _card_map.setdefault(chat, {})
    # per-draft photos (best-effort — failure never blocks the card)
    for d in rows[:DRAFTS_PAGE]:
        if not d.get("image"):
            continue
        caption = (f"draft #{d['id']} — {(d.get('text') or '')[:180]}\n"
                   f"reply /approve {d['id']} or /reject {d['id']}")
        for chat in allowed_chats():
            if send_photo(chat, d["image"], caption)["ok"]:
                continue
            send_message(chat, f"draft #{d['id']} (image attached — view it in Inbox)")
    return result
```

Card-map recording: `send_message` returns `message_id` — but `notify()` broadcasts internally and drops it. Extend `notify` to return the last `message_id` per chat is invasive; instead, record the map inside `send_message` is wrong (it doesn't know draft context). Pragmatic v0.6: record the map in `notify_new_drafts` by re-sending… no. **Correct minimal approach:** give `notify` an optional `card_drafts` param:

```python
def notify(text: str, card_drafts: list[int] | None = None) -> dict:
    """Broadcast to every allowed chat (digest cron + approval cards).
    Rate-limited per message; failures are logged, never raised.
    card_drafts: when set, the sent card's message_id is recorded for
    reply-with-photo targeting (chat → message_id → draft ids)."""
    chats = allowed_chats()
    if not is_enabled():
        return {"ok": False, "sent": 0, "chats": 0, "error": "telegram disabled"}
    sent = 0
    for c in chats:
        r = send_message(c, text)
        if r["ok"]:
            sent += 1
            if card_drafts is not None and r.get("message_id"):
                _card_map.setdefault(c, {})[r["message_id"]] = list(card_drafts)
    return {"ok": sent > 0, "sent": sent, "chats": len(chats), "error": None}
```

And in `notify_new_drafts` call `notify(drafts_card(rows), card_drafts=previewed)`. Drop the `for chat … setdefault` loop from (d). `_card_map` shape: `{chat_id: {message_id: [draft_ids]}}`.

- [ ] **Step 5: Run tests — pass**

Run: `.venv\Scripts\python -m pytest tests/test_telegram.py -q`
Expected: all PASS (existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add openstanley/integrations/telegram.py tests/test_telegram.py
git commit -m "tg: approval cards push their image (sendPhoto/GIF-as-document, text fallback)"
```

---

### Task 2: TG inbound — attach a photo by reply or /img caption

**Files:**
- Modify: `openstanley/integrations/telegram.py` (`_handle_update` ~line 995, `HELP_TEXT` ~line 56, inbound section)
- Test: `tests/test_telegram.py`

- [ ] **Step 1: Extend the fake with getFile + file GET**

Inside `FakeTG.post`, before the final `return _R(200, {"ok": True, ...})`:

```python
        if method == "getFile":
            return _R(200, {"ok": True, "result": {"file_path": "photos/file_1.jpg"}})
```

And add a `get` method to the class (the photo-bytes source):

```python
    def get(self, url, timeout=None, **kw):
        self.calls.append((url, "GET-file", {}))
        return _RBytes(b"\xff\xd8 fake jpeg bytes")
```

Add a tiny `_RBytes` near `_R` in the test file:

```python
class _RBytes:
    def __init__(self, content: bytes):
        self.status_code = 200
        self.content = content
```

- [ ] **Step 2: Write the failing tests**

```python
def _photo_update(caption: str = "", reply_to: int | None = None) -> dict:
    msg = {"chat": {"id": CHAT},
           "photo": [{"file_id": "f1", "file_size": 100},
                     {"file_id": "f2", "file_size": 4000}],
           "caption": caption}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": msg}


def test_photo_caption_img_attaches(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = FakeTG()
    monkeypatch.setattr(tg, "httpx", fake)
    d = db.add_draft(text="target draft", acct=1)
    tg._handle_update(cfg_for_tests(), _photo_update(caption=f"/img {d}"))
    row = db.get_draft(d)
    assert row["image"] and row["image"].startswith("media_")
    assert (tmp_path / row["image"]).exists()
    assert any("attached" in t.lower() for _c, t in fake.sent())


def test_photo_reply_to_card_attaches(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = FakeTG()
    monkeypatch.setattr(tg, "httpx", fake)
    d = db.add_draft(text="card draft", acct=1)
    tg._card_map.clear()
    tg._card_map[CHAT] = {777: [d]}
    tg._handle_update(cfg_for_tests(), _photo_update(reply_to=777))
    assert db.get_draft(d)["image"]


def test_photo_reply_ambiguous_card_asks_for_caption(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = FakeTG()
    monkeypatch.setattr(tg, "httpx", fake)
    d1, d2 = db.add_draft(text="a", acct=1), db.add_draft(text="b", acct=1)
    tg._card_map.clear()
    tg._card_map[CHAT] = {777: [d1, d2]}
    tg._handle_update(cfg_for_tests(), _photo_update(reply_to=777))
    assert db.get_draft(d1)["image"] is None
    assert any("/img" in t for _c, t in fake.sent())


def test_photo_no_target_gets_hint(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = FakeTG()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._card_map.clear()
    tg._handle_update(cfg_for_tests(), _photo_update())
    assert any("/img" in t for _c, t in fake.sent())


def test_photo_disallowed_chat_ignored(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = FakeTG()
    monkeypatch.setattr(tg, "httpx", fake)
    upd = _photo_update()
    upd["message"]["chat"]["id"] = 666999
    tg._handle_update(cfg_for_tests(), upd)
    assert fake.sent() == [] or len(fake.sent()) <= 1  # gate reply only, no attach


def test_video_document_declined(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = FakeTG()
    monkeypatch.setattr(tg, "httpx", fake)
    upd = {"update_id": 1, "message": {"chat": {"id": CHAT},
           "document": {"file_id": "f1", "mime_type": "video/mp4"}}}
    tg._handle_update(cfg_for_tests(), upd)
    assert any("photos only" in t.lower() for _c, t in fake.sent())
```

(`CFG` is the module-level `Config()` instance defined at the top of `tests/test_telegram.py` — reuse it directly.)

- [ ] **Step 3: Run — expect failure**

Run: `.venv\Scripts\python -m pytest tests/test_telegram.py -q -k photo`
Expected: FAIL — photos are currently ignored (`_handle_update` early-returns).

- [ ] **Step 4: Implement**

(a) `HELP_TEXT` — insert after the `/reject` line:

```python
    "/img <id> — attach a photo to a draft (send the photo with this caption,\n"
    "           or just reply to a draft card with a photo)\n"
```

(b) Inbound section (before `_handle_update`):

```python
def _download_tg_photo(token: str, file_id: str) -> bytes:
    """Largest photo → bytes. Two calls: getFile → GET file. Raises on any
    failure — the caller turns it into a human message."""
    r = _api(token, "getFile", {"file_id": file_id})
    if not (200 <= r.status_code < 300):
        raise RuntimeError(f"getFile HTTP {r.status_code}")
    fp = (r.json().get("result") or {}).get("file_path")
    if not fp:
        raise RuntimeError("no file_path in getFile response")
    fr = httpx.get(FILE_URL.format(token=token, path=fp), timeout=HTTP_TIMEOUT_S)
    if not (200 <= fr.status_code < 300):
        raise RuntimeError(f"download HTTP {fr.status_code}")
    return fr.content


def _save_tg_photo(token: str, msg: dict) -> str:
    """Download the largest photo size and store it in MEDIA_DIR with the
    standard media_<ts>_<hex> name. Returns the stored name."""
    sizes = msg.get("photo") or []
    biggest = max(sizes, key=lambda s: s.get("file_size") or 0)
    if (biggest.get("file_size") or 0) > MAX_IMAGE_BYTES:
        raise ValueError(f"photo too large (max {MAX_IMAGE_BYTES // (1024*1024)}MB)")
    data = _download_tg_photo(token, biggest["file_id"])
    import secrets, time as _time
    name = f"media_{int(_time.time())}_{secrets.token_hex(3)}.jpg"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(data)
    return name


def _attach_photo(cfg: Config, chat_id: int, msg: dict, target: int | None,
                  hint: str) -> None:
    """Shared tail: attach the photo in `msg` to draft `target` (or reply
    with `hint` when no target), and say what happened."""
    token = bot_token()
    if target is None:
        send_message(chat_id, hint)
        return
    d = db.get_draft(target)
    if not d or d.get("status") not in ("draft", "approved"):
        send_message(chat_id, f"Draft #{target} isn't waiting — /drafts lists them.")
        return
    try:
        name = _save_tg_photo(token, msg)
    except ValueError as e:
        send_message(chat_id, str(e))
        return
    except Exception as e:  # noqa: BLE001
        db.log("telegram", f"photo download failed: {_scrub(str(e), token)}",
               level="warn")
        send_message(chat_id, "Couldn't fetch the photo from Telegram — try again.")
        return
    db.update_draft(target, image=name)
    db.log("telegram", f"photo attached to draft #{target} ({name})")
    send_message(chat_id, f"Attached to draft #{target} ✓")
```

(c) `_handle_update` — replace the early-return block (lines 996-1000) so photos flow:

```python
def _handle_update(cfg: Config, upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = int((msg.get("chat") or {}).get("id") or 0)
    text = str(msg.get("text") or "").strip()
    photo = msg.get("photo")
    if not chat_id or (not text and not photo):
        if msg.get("document") and chat_id:
            pass  # handled below the auth gate
        else:
            return
    db.log("telegram", f"inbound from chat {chat_id}: "
                       f"{(text or 'photo')[:60]!r}", level="info")

    denied = _auth_reply(chat_id)
    if denied is not None:
        if denied:  # empty string = stay silent
            send_message(chat_id, denied)
        return

    if not text and (photo or msg.get("document")):
        if photo:
            _handle_photo(cfg, chat_id, msg)
        else:
            send_message(chat_id, "I can only attach photos for now — "
                                  "videos aren't supported yet.")
        return
    if not text:
        return
    # … existing command/chat dispatch unchanged from here …
```

And the photo router (place above `_handle_update`):

```python
def _handle_photo(cfg: Config, chat_id: int, msg: dict) -> None:
    """Photo arrived. Target draft: /img <id> caption > reply-to-card."""
    cap = str(msg.get("caption") or "").strip()
    cmd = parse_command(cap) if cap.startswith("/") else None
    target: int | None = None
    hint = ("Which draft? Send the photo again with a caption like "
            "`/img 12` — /drafts lists the ids.")
    if cmd and cmd[0] == "img":
        target = _int_arg(cmd[1], "img")
        if target < 0:
            send_message(chat_id, "Use `/img <id>` — e.g. `/img 12`.")
            return
    else:
        replied = (msg.get("reply_to_message") or {}).get("message_id")
        ids = (tg_cards := _card_map.get(chat_id) or {}).get(replied) if replied else None
        if ids is None:
            target = None
        elif len(ids) == 1:
            target = ids[0]
        else:
            send_message(chat_id, "That card lists several drafts — send the "
                                  "photo with a caption like `/img 12`.")
            return
    _attach_photo(cfg, chat_id, msg, target, hint)
```

(Delete the `tg_cards` walrus if it reads badly — plain `(_card_map.get(chat_id) or {}).get(replied)`.)

- [ ] **Step 5: Run tests — pass**

Run: `.venv\Scripts\python -m pytest tests/test_telegram.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add openstanley/integrations/telegram.py tests/test_telegram.py
git commit -m "tg: attach photos to drafts — /img caption or reply-to-card, /help documents it"
```

---

### Task 3: `/api/chat/draft` accepts an image

**Files:**
- Modify: `openstanley/server/__main__.py` (`ChatDraftBody` line 813, `chat_draft_ep` line 845)
- Modify: `openstanley/gen/chat.py` (`draft_from_chat` line 383)
- Test: `tests/test_tools.py` or a new `tests/test_chat_draft_media.py`

- [ ] **Step 1: Write the failing test** — `tests/test_chat_draft_media.py`:

```python
"""POST /api/chat/draft with image — Write-chat candidate save carries media."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from fastapi.testclient import TestClient                          # noqa: E402
from openstanley.server.__main__ import app, MEDIA_DIR            # noqa: E402


def test_chat_draft_with_image():
    (MEDIA_DIR / "media_test_ok.png").write_bytes(b"\x89PNG test")
    with TestClient(app) as client:
        r = client.post("/api/chat/draft",
                        json={"text": "hello with media",
                              "image": "media_test_ok.png"})
    assert r.status_code == 200, r.text
    did = r.json()["draft_id"]
    assert db.get_draft(did)["image"] == "media_test_ok.png"


def test_chat_draft_bad_image_name_rejected():
    with TestClient(app) as client:
        r = client.post("/api/chat/draft",
                        json={"text": "x", "image": "../evil.png"})
    assert r.status_code == 400


def test_chat_draft_missing_image_rejected():
    with TestClient(app) as client:
        r = client.post("/api/chat/draft",
                        json={"text": "x", "image": "nope.png"})
    assert r.status_code == 404
```

(Conftest sandboxes the DB via `OPENSTANLEY_TEST_DB`. `MEDIA_DIR` is imported from the server module so the test writes where the endpoint looks; media tests here write uniquely-named files and are harmless if `MEDIA_DIR` is the real one — prefer matching `tests/test_telegram.py`'s `tmp_path` sandbox if the existing suite has one for MEDIA_DIR.)

- [ ] **Step 2: Run — expect failure**

Run: `.venv\Scripts\python -m pytest tests/test_chat_draft_media.py -q`
Expected: FAIL — `image` is not a valid field (pydantic ignores extras → row has `image=None`) or 422.

- [ ] **Step 3: Implement**

`openstanley/server/__main__.py` line 813:

```python
class ChatDraftBody(BaseModel):
    text: str
    image: str | None = None
```

Line 845 endpoint — add the same validation `attach_media` uses, then pass through:

```python
@app.post("/api/chat/draft")
async def chat_draft_ep(body: ChatDraftBody):
    from ..gen import chat as chat_mod
    if body.image and ("/" in body.image or "\\" in body.image or ".." in body.image):
        raise HTTPException(400, "bad image name")
    if body.image and not (MEDIA_DIR / body.image).exists():
        raise HTTPException(404, "no such media file")
    did = await asyncio.to_thread(chat_mod.draft_from_chat, cfg,
                                  body.text, body.image)
    return {"ok": True, "draft_id": did}
```

`openstanley/gen/chat.py` line 383 — signature + the `db.add_draft` call (line 399):

```python
def draft_from_chat(cfg: Config, text: str, image: str | None = None) -> int:
```

```python
    did = db.add_draft(text=text, kind="post", temperature="chat",
                       meta=meta, image=image)
```

- [ ] **Step 4: Run tests — pass**

Run: `.venv\Scripts\python -m pytest tests/test_chat_draft_media.py tests/test_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add openstanley/server/__main__.py openstanley/gen/chat.py tests/test_chat_draft_media.py
git commit -m "chat: /api/chat/draft carries an image — Write candidates save with media"
```

---

### Task 4: Write composer — stage an image onto the next saved draft

**Files:**
- Modify: `web/src/pages/Write.tsx` (page-level state near line 637; chip UI above `<PromptBar>` line 836; thread through `AssistantTurn` line 824 → `CandidateApproval` line 374)
- Build: `web/dist` via `npm run build`

No i18n additions — reuse existing keys: `inbox.composeImage` ("Attach image"), `inbox.uploading`, `inbox.removeImage` (verify exact key names with `grep -n "removeImage\|composeImage" web/src/lib/i18n.ts` first; if `removeImage` is absent, reuse `inbox.composeImage` for the aria-label).

- [ ] **Step 1: Add staged-image state + attach UI to the page component**

In `Write.tsx` main component, next to the existing `input`/`busy` state (~line 637):

```tsx
  const [stagedImage, setStagedImage] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const stageFile = async (f: File | undefined) => {
    if (!f) return;
    try {
      setUploading(true);
      const r = await uploadMedia(f);
      setStagedImage(r.name);
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setUploading(false);
    }
  };
```

(Add `uploadMedia` to the existing api-import list at the top of the file; `useRef` to the React import if absent; `toast`/`errMsg` are already used in `CandidateApproval` — hoist imports if they're local to it.)

- [ ] **Step 2: Chip UI above `<PromptBar>`** (line ~835, inside the `<div className="pb-4 pt-3">`):

```tsx
        <div className="pb-4 pt-3">
          {stagedImage ? (
            <div className="mb-2 inline-flex items-center gap-2 rounded-chip bg-inset px-2 py-1">
              <img src={`/api/media/${stagedImage}`} alt="" className="size-6 rounded object-cover" />
              <span className="max-w-40 truncate font-mono text-[11px] text-ink-2">{stagedImage}</span>
              <button
                type="button"
                onClick={() => setStagedImage(null)}
                className="text-ink-3 hover:text-ink"
                aria-label={t('inbox.composeImage')}
              >
                ✕
              </button>
            </div>
          ) : null}
          <PromptBar
            ...
            footerHint={t('write.sendHint')}
            onAttach={stagedImage === null && !uploading ? () => fileRef.current?.click() : undefined}
            attachLabel={uploading ? t('inbox.uploading') : t('inbox.composeImage')}
          />
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            className="hidden"
            onChange={(e) => { void stageFile(e.target.files?.[0]); e.target.value = ''; }}
          />
        </div>
```

- [ ] **Step 3: PromptBar optional attach button** — `web/src/components/bui/components/PromptBar.tsx`: add optional props `onAttach?: () => void` and `attachLabel?: string`; render a paperclip button (📎) at the left of the action row, `disabled`/hidden when `onAttach` is undefined:

```tsx
  {onAttach ? (
    <button
      type="button"
      onClick={onAttach}
      title={attachLabel}
      className="rounded-chip px-2 py-1 text-ink-3 transition-colors hover:bg-hover-2 hover:text-ink"
      aria-label={attachLabel}
    >📎</button>
  ) : null}
```

(Match the file's existing button classes/action-row markup — inspect first, blend in.)

- [ ] **Step 4: Thread stagedImage into the save** — `AssistantTurn` call (line 824) gains `stagedImage={stagedImage}` and `onImageConsumed={() => setStagedImage(null)}`; the `AssistantTurn` component signature adds both props; its `<CandidateApproval>` call (line 374) forwards them; `CandidateApproval` (line 160) accepts them and its `onAccept` becomes:

```tsx
      onAccept={async () => {
        try {
          const r = await apiPost<{ ok: boolean; draft_id: number }>('chat/draft', {
            text: candidate.text,
            image: stagedImage ?? undefined,
          });
          setSavedId(r.draft_id);
          onImageConsumed?.();
          toast.success(t('write.draftSaved', { id: r.draft_id }));
        } catch (e) {
          toast.error(t('write.draftSaveFailed', { msg: errMsg(e) }));
        }
      }}
```

(Verified: `apiPost` → `JSON.stringify` omits `undefined` properties, so `image: stagedImage ?? undefined` sends the key only when an image is staged.)

- [ ] **Step 5: Typecheck + build**

Run: `cd web && npm run build`
Expected: exit 0, no TS errors.

- [ ] **Step 6: Commit**

```bash
git add web/src/pages/Write.tsx web/src/components/bui/components/PromptBar.tsx
git commit -m "write: paperclip in the chat composer — staged image rides the candidate into the draft"
```

---

### Task 5: Full verification + ship

- [ ] **Step 1: Full suite**

Run: `.venv\Scripts\python -m pytest -q`
Expected: all PASS (350 + ~10 new).

- [ ] **Step 2: Web build already done in Task 4 — verify `web/dist` freshness**

Run: `ls -la web/dist/index.html` (timestamp after the build).

- [ ] **Step 3: Restart the live server** (it runs pre-v0.6 code)

```bash
powershell -Command "Get-NetTCPConnection -LocalPort 7878 -State Listen | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }"
cd D:\Ai\openstanley && .venv\Scripts\python -m openstanley.server   # run_in_background
```

- [ ] **Step 4: Live smoke**

```bash
curl -s http://127.0.0.1:7878/api/health
curl -s -X POST http://127.0.0.1:7878/api/telegram/test | head -c 200
```

Expected: `{"ok":true,...}` and a TG ok payload. Then (manual, user-side): send a photo with caption `/img <id>` in the whitelisted TG chat.

- [ ] **Step 5: PROGRESS.md line + final commit**

Append one line to `PROGRESS.md` (repo convention) and commit:

```bash
git add PROGRESS.md
git commit -m "progress: v0.6 media round-trip — TG cards/photos + Write attach live"
```
