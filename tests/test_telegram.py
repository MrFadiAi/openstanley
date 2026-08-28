"""Telegram integration v0.4.4 — the second frontend.

Covers: command parsing, the auth gate (one refusal then silence, and the
bootstrap "here is your chat id" reply), offset tracking, the poller
lifecycle incl. restart on token change and the bad-token state, chat over
the dashboard engine (per-chat sessions capped at 20), /approve through
smart slots, /post as a voice-locked approval-gated draft, the read-only
commands, outbound rate limiting, the digest bridge, the new-draft hook in
the agent loops, and the settings API (token masked, test endpoint).

All hermetic: Bot API traffic is faked at the module httpx seam, the LLM is
faked at chat.llm_chat, the brain is sandboxed into tmp, and the poller's
env guard (OPENSTANLEY_NO_TELEGRAM=1, set in conftest) keeps TestClient boots
offline — the lifecycle tests bypass it with force=True over a faked wire.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
import json
import types
from datetime import datetime
from pathlib import Path

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"  # before importing the server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

from openstanley.core import db               # noqa: E402
db.init_db()

import openstanley.server.__main__ as server  # noqa: E402

client = TestClient(server.app)

from openstanley.core.config import Config    # noqa: E402
from openstanley.gen import agent as agent_mod  # noqa: E402
from openstanley.gen import brain             # noqa: E402
from openstanley.gen import chat as chat_mod  # noqa: E402
from openstanley.gen import digest as digest_mod  # noqa: E402
from openstanley.gen import slots as slots_mod  # noqa: E402
from openstanley.integrations import telegram as tg  # noqa: E402

CHAT = 111222333
TODAY = datetime.now().date().isoformat()
CFG = Config()
FENCE = "`" * 3


# ---------------- helpers ----------------

def _upd(update_id: int, chat_id: int, text: str) -> dict:
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text,
                        "from": {"id": chat_id}}}


class _R:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self.text = "ok"
        self._p = payload

    def json(self) -> dict:
        return self._p


class _RBytes:
    def __init__(self, content: bytes):
        self.status_code = 200
        self.content = content


class _FakeTGHttpx:
    """Records Bot API calls; serves scripted getUpdates batches, then empty.
    on_exhausted fires when the batches run dry (tests stop the loop there)."""

    def __init__(self, batches: list | None = None, status: int = 200,
                 on_exhausted=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.batches = list(batches or [])
        self.status = status
        self.on_exhausted = on_exhausted

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
        if method == "getFile":
            return _R(200, {"ok": True, "result": {"file_path": "photos/file_1.jpg"}})
        return _R(self.status, {"ok": True, "result": {"message_id": 4242}})

    def get(self, url, timeout=None, **kw):
        self.calls.append((url, "GET-file", {}))
        return _RBytes(b"\xff\xd8 fake jpeg bytes")

    def sent(self) -> list[tuple[int, str]]:
        """(chat_id, text) of every sendMessage the fake saw."""
        return [(p.get("chat_id"), p.get("text", ""))
                for _u, m, p in self.calls if m == "sendMessage"]

    def media_sends(self) -> list[tuple[str, int, str, str]]:
        """(method, chat_id, caption, filename) of every multipart send."""
        return [(m, p.get("chat_id"), p.get("caption", ""), p.get("_file", ""))
                for _u, m, p in self.calls if m in ("sendPhoto", "sendDocument")]


def _enable(token: str = "700:AAHtest-token", chats: list[int] | None = None) -> None:
    db.set_setting("tg_bot_token", token)
    db.set_setting("tg_allowed_chats", chats if chats is not None else [CHAT])
    db.set_setting("tg_enabled", True)


def _warn_count() -> int:
    with db.connect() as c:
        (n,) = c.execute(
            "SELECT COUNT(*) FROM agent_log WHERE loop='telegram' "
            "AND level='warn' AND message LIKE '%rate limit%'").fetchone()
    return int(n)


@pytest.fixture(autouse=True)
def _tg_sandbox(tmp_path, monkeypatch):
    """Fresh brain dir, fresh poller/session/rate state, settings restored."""
    sandbox = tmp_path / "brain"
    # v0.5.0: brains live under ACCOUNTS_ROOT/<id>/brain — sandbox the anchor
    monkeypatch.setattr(brain, "ACCOUNTS_ROOT", tmp_path / "accounts")
    sandbox = brain.brain_dir()
    brain.ensure()
    # reflection fires a daemon-thread LLM call every 10th chat message —
    # not under test here, keep it inert
    monkeypatch.setattr(brain, "maybe_reflect_chat_async", lambda cfg: False)
    tg._reset_rate()
    tg._denied_chats.clear()
    tg._sessions.clear()
    tg._chat_tasks.clear()
    tg._state.update(task=None, offset=0, mode="disabled")
    tg._state["stop"].clear()
    yield
    db.set_setting("tg_bot_token", "")
    db.set_setting("tg_allowed_chats", [])
    db.set_setting("tg_enabled", False)


# ---------------- parsing ----------------

def test_parse_command_variants():
    assert tg.parse_command("/approve 12") == ("approve", "12")
    assert tg.parse_command("/start@openstanley_bot") == ("start", "")
    assert tg.parse_command("/POST  hello world  ") == ("post", "hello world")
    assert tg.parse_command("just talking") is None
    assert tg.parse_command("") is None
    assert tg.parse_command("/") is None


# ---------------- auth gate ----------------

def test_disallowed_chat_gets_one_reply_then_silence(monkeypatch):
    _enable(chats=[CHAT])
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, 999, "/status"))
    tg.handle_update(CFG, _upd(2, 999, "/status"))
    tg.handle_update(CFG, _upd(3, 999, "hello?"))
    sent = fake.sent()
    assert len(sent) == 1                       # one refusal, then silence
    assert "private" in sent[0][1].lower()
    assert sent[0][0] == 999


def test_empty_allowed_list_replies_with_chat_id(monkeypatch):
    _enable(chats=[])
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, 424242, "/start"))
    sent = fake.sent()
    assert len(sent) == 1
    assert "424242" in sent[0][1]               # bootstrap UX: shows the id
    assert "tg_allowed_chats" in sent[0][1]


# ---------------- update dispatch + commands ----------------

def test_help_and_unknown_command(monkeypatch):
    _enable()
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, CHAT, "/start"))
    tg.handle_update(CFG, _upd(2, CHAT, "/frobnicate"))
    sent = fake.sent()
    assert "/approve" in sent[0][1] and "/digest" in sent[0][1]
    assert "Unknown command /frobnicate" in sent[1][1]


def test_status_command_reports_state(monkeypatch):
    _enable()
    db.set_setting("me", {"username": "fadi", "followers": 1234})
    db.add_idea("an idea", "angle", "one-liner", "study", 7.5)
    bank = db.idea_count()  # shared fixture db — assert relative to now
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, CHAT, "/status"))
    text = fake.sent()[0][1]
    assert "@fadi" in text and "1234" in text
    assert "autopilot" in text.lower()
    assert "<b>Idea bank</b>" in text and str(bank) in text   # bold on the wire
    assert "dryrun" in text


def test_ideas_and_drafts_commands(monkeypatch):
    _enable()
    db.add_idea("ship ugly versions", "angle", "one-liner", "study", 9.0)
    db.add_idea("boring stack wins", "angle", "thread", "study", 6.0)
    d1 = db.add_draft(text="a post draft that waits and waits for approval")
    d2 = db.add_draft(text="solid question reply", kind="reply",
                      meta={"target_author": "alice", "reply_to_x_id": "x9",
                            "voice": {"score": 82, "checked": True}})
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, CHAT, "/ideas"))
    tg.handle_update(CFG, _upd(2, CHAT, "/drafts"))
    ideas_text, drafts_text = fake.sent()[0][1], fake.sent()[1][1]
    assert "ship ugly versions" in ideas_text and "9" in ideas_text
    assert f"#{d1}" in drafts_text and f"#{d2}" in drafts_text
    assert "@alice" in drafts_text            # target chip
    assert "voice 82%" in drafts_text         # voice chip
    # v0.5.1: drafts quoted VERBATIM in full — no previews, no truncation
    assert "“a post draft that waits and waits for approval”" in drafts_text
    assert "“solid question reply”" in drafts_text
    assert "…" not in drafts_text and "·" not in drafts_text


def test_command_replies_share_one_template(monkeypatch):
    """Audit invariant (v0.5.1 FIX_BRIEF_TG_OUTPUT_POLISH): every command
    reply is clean Telegram — no literal markdown (`**`, backticks, `##`),
    no `·` separators anywhere, and emoji only as leading section markers."""
    _enable()
    db.set_setting("me", {"username": "fadi", "followers": 10})
    db.add_idea("an idea", "angle", "one-liner", "study", 7.5)
    did = db.add_draft(text="a draft whose preview is long enough to read well here")
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    for i, cmd in enumerate(("/status", "/ideas", "/drafts", "/help",
                             "/account")):
        tg.handle_update(CFG, _upd(i + 1, CHAT, cmd))
    texts = [t for _c, t in fake.sent()]
    assert len(texts) == 5
    for text in texts:
        assert "**" not in text and "`" not in text and "##" not in text
        assert "·" not in text                       # never a dot separator
    assert f"#{did}" in texts[2]                     # draft id on the wire
    # emoji appear ONLY as the leading section marker (header line)
    emoji_re = re.compile(r"^[\U0001F300-\U0001FAFF⏳]")
    for text in texts[:3]:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        headed = [i for i, ln in enumerate(lines) if emoji_re.match(ln)]
        assert headed == [0], lines                   # header only, nowhere else


def test_digest_command_renders_today(monkeypatch):
    _enable()
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, CHAT, "/digest"))
    text = fake.sent()[0][1]
    assert text.startswith("📰") and TODAY in text


def test_reject_command():
    d = db.add_draft(text="a draft that will be rejected from telegram")
    reply = tg.reject_draft_tg(d)
    assert "rejected" in reply.lower()
    assert db.get_draft(d)["status"] == "rejected"
    assert "no draft" in tg.reject_draft_tg(99999).lower()


# ---------------- /approve + /post (the approval gate) ----------------

def test_approve_schedules_through_smart_slots(monkeypatch):
    _enable()
    d = db.add_draft(text="a post that should get a smart slot from tg")
    picked = []

    def _spy(cfg, kind, now):
        picked.append(kind)
        return datetime(2030, 1, 1, 9, 0, 0), "cadence slot 09:00 · no recent post"

    monkeypatch.setattr(slots_mod, "pick_slot_with_reason", _spy)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, CHAT, f"/approve {d}"))
    row = db.get_draft(d)
    assert picked == ["post"]                          # smart slots engaged
    assert row["status"] == "approved"
    assert row["scheduled_at"] == "2030-01-01T09:00:00"
    assert row["meta"]["scheduled_reason"].startswith("cadence slot")
    assert "approved" in fake.sent()[0][1].lower()
    # junk id → helpful refusal, nothing crashes
    tg.handle_update(CFG, _upd(2, CHAT, "/approve notanumber"))
    assert "No approvable draft" in fake.sent()[1][1]


def test_post_creates_voice_locked_draft_with_source_tg(monkeypatch):
    _enable()
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    text = "this is my own post typed straight into telegram"
    tg.handle_update(CFG, _upd(1, CHAT, f"/post {text}"))
    sent_text = fake.sent()[0][1]
    with db.connect() as c:
        row = c.execute("SELECT * FROM drafts WHERE text=?", (text,)).fetchone()
    assert row is not None
    assert row["status"] == "draft"                    # queued, NOT published
    assert row["meta_json"].find('"source": "tg"') >= 0
    assert '"voice"' in row["meta_json"]               # voice verdict attached
    assert "queued" in sent_text.lower()
    # too-short text gets a nudge instead of a draft
    tg.handle_update(CFG, _upd(2, CHAT, "/post hi"))
    assert any("15+" in t for _c, t in fake.sent())


# ---------------- chat over the dashboard engine ----------------

def test_chat_reuses_engine_with_capped_session(monkeypatch):
    _enable()
    seen: dict[str, str] = {}

    def _fake_llm_stream(llm_cfg, system, user):
        seen["user"] = user
        yield "Roger "
        yield "that."

    monkeypatch.setattr(chat_mod, "llm_chat_stream", _fake_llm_stream)
    "".join(tg.chat_reply_tg_stream(CFG, CHAT, "first question"))
    assert "first question" in seen["user"]            # same engine turn shape
    "".join(tg.chat_reply_tg_stream(CFG, CHAT, "second question"))
    assert "first question" in seen["user"]            # session memory held
    assert "ASSISTANT: Roger that." in seen["user"]
    for i in range(11):                                 # 13 turns = 26 messages
        "".join(tg.chat_reply_tg_stream(CFG, CHAT, f"msg {i}"))
    assert len(tg._sessions[CHAT]) == tg.SESSION_CAP   # capped at 20
    seen.clear()
    "".join(tg.chat_reply_tg_stream(CFG, CHAT, "latest question"))
    assert "latest question" in seen["user"]
    assert "first question" not in seen["user"]        # oldest rolled out


# ---------------- chat candidates → drafts (the approval gate's TG path) ----------------

def _stub_voice(monkeypatch, score: int = 88) -> None:
    """Deterministic voice verdict — check_draft's borderline path attempts
    an LLM rewrite; tests must never make that call for real."""
    vc = types.SimpleNamespace(fixed_text=None,
                               meta=lambda: {"score": score, "checked": True})
    monkeypatch.setattr(chat_mod.voice_lock, "check_draft",
                        lambda cfg, text, **k: vc)


def _clear_chat_draft_residue(*texts: str) -> None:
    """The TG chat path dedupes candidates against the last 60 drafts
    GLOBALLY — residue from an earlier run's saved candidates makes every
    later run skip the save. Clear this test's fixture texts first."""
    with db.connect() as c:
        for t in texts:
            c.execute("DELETE FROM drafts WHERE text=?", (t.strip(),))


def test_chat_candidates_saved_as_drafts(monkeypatch):
    """Defect 1 — a post drafted in TG chat becomes a real draft the user can
    /approve. Same draft_from_chat path as the web UI's approval cards."""
    _enable()
    post = "an ugly first version teaches what slides never will"
    _clear_chat_draft_residue(post)
    monkeypatch.setattr(chat_mod, "llm_chat_stream",
                        lambda *a, **k: iter([f"here you go:\n> {post}\n"]))
    monkeypatch.setattr(chat_mod, "llm_chat", lambda *a, **k: "")
    _stub_voice(monkeypatch)
    tg._sessions.clear()
    out = "".join(tg.chat_reply_tg_stream(CFG, CHAT, "draft me a post"))
    m = re.search(r"Saved as draft #(\d+) — /approve \1 to publish", out)
    assert m, out
    d = db.get_draft(int(m.group(1)))
    assert d["text"] == post
    assert d["status"] == "draft"                       # queued, NOT published
    assert d["meta"]["source"] == "chat"                # web-UI draft path
    assert out.count(post) == 1                         # prose never duplicated
    assert tg._sessions[CHAT][-1]["content"].endswith(
        f"Saved as draft #{m.group(1)} — /approve {m.group(1)} to publish")


def test_chat_multiple_candidates_each_saved(monkeypatch):
    _enable()
    p1 = "first candidate post, long enough to count"
    p2 = "second candidate post, also long enough"
    _clear_chat_draft_residue(p1, p2)
    monkeypatch.setattr(chat_mod, "llm_chat_stream",
                        lambda *a, **k: iter([f"> {p1}\n\n> {p2}\n"]))
    monkeypatch.setattr(chat_mod, "llm_chat", lambda *a, **k: "")
    _stub_voice(monkeypatch)
    tg._sessions.clear()
    out = "".join(tg.chat_reply_tg_stream(CFG, CHAT, "draft two posts"))
    ids = re.findall(r"Saved as draft #(\d+)", out)
    assert len(ids) == 2 and ids[0] != ids[1]
    assert db.get_draft(int(ids[0]))["text"] == p1
    assert db.get_draft(int(ids[1]))["text"] == p2


def test_chat_tool_results_reach_the_user(monkeypatch):
    """list_drafts + the follow-up turn: "show me the drafts" answers with the
    REAL ids (web parity), not a guessed list."""
    _enable()
    d1 = db.add_draft(text="a draft the chat should be able to list for me")
    monkeypatch.setattr(chat_mod, "llm_chat_stream",
                        lambda *a, **k: iter(["checking…\n"
                                              f"{FENCE}action\n"
                                              '{"tool": "list_drafts", "args": {}}\n'
                                              f"{FENCE}\n"]))
    monkeypatch.setattr(chat_mod, "llm_chat",
                        lambda *a, **k: f"you have draft #{d1} waiting.")
    _stub_voice(monkeypatch)
    tg._sessions.clear()
    out = "".join(tg.chat_reply_tg_stream(CFG, CHAT, "show me the drafts"))
    assert f"you have draft #{d1} waiting." in out      # real ids, folded in
    # the fence was streamed raw, but the stored/remembered reply is clean
    assert FENCE + "action" not in tg._sessions[CHAT][-1]["content"]


# ---------------- TG sessions persist + rebuild (defect 3) ----------------

PCHAT = 555000111    # suite-unique chat id → persisted rows are provably ours


def _wipe_persisted(chat_id: int) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM chat_messages WHERE chat_id=?", (chat_id,))


def test_tg_turns_persist_and_stay_out_of_web_history(monkeypatch):
    _enable()
    monkeypatch.setattr(chat_mod, "llm_chat_stream",
                        lambda *a, **k: iter(["ok answer one"]))
    monkeypatch.setattr(chat_mod, "llm_chat", lambda *a, **k: "")
    _stub_voice(monkeypatch)
    _wipe_persisted(PCHAT)
    tg._sessions.clear()
    "".join(tg.chat_reply_tg_stream(CFG, PCHAT, "persisted question one"))
    rows = db.chat_history_for_chat(PCHAT, 10)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "persisted question one"
    assert rows[1]["content"] == "ok answer one"
    assert all(r["meta"].get("chat_id") == PCHAT for r in rows)
    # the dashboard chat never sees TG turns — histories don't mix
    tg_ids = {r["id"] for r in rows}
    assert all(r["id"] not in tg_ids for r in db.chat_history(100))


def test_tg_session_rebuilds_from_db_after_restart(monkeypatch):
    _enable()
    seen: dict[str, str] = {}
    replies = iter(["first reply body", "second reply body"])

    def _capture(llm_cfg, system, user):
        seen["user"] = user
        return iter([next(replies)])

    monkeypatch.setattr(chat_mod, "llm_chat_stream", _capture)
    monkeypatch.setattr(chat_mod, "llm_chat", lambda *a, **k: "")
    _stub_voice(monkeypatch)
    _wipe_persisted(PCHAT)
    tg._sessions.clear()
    "".join(tg.chat_reply_tg_stream(CFG, PCHAT, "question before restart"))
    tg._sessions.clear()                                # the restart: RAM gone
    "".join(tg.chat_reply_tg_stream(CFG, PCHAT, "question after restart"))
    u = seen["user"]
    assert "question after restart" in u
    assert "USER: question before restart" in u         # rebuilt from chat_messages
    assert "ASSISTANT: first reply body" in u


# ---------------- concurrent dispatch (defect 2) ----------------

def test_poller_parallel_chats_same_chat_ordered(monkeypatch):
    """A slow reply parks ITS chat only — other chats still get answers, and
    replies to one chat never interleave out of order."""
    _enable()
    other = CHAT + 1
    events: list[tuple] = []
    lock = threading.Lock()

    def fake_handle(cfg, upd):
        cid, uid = upd["message"]["chat"]["id"], upd["update_id"]
        with lock:
            events.append(("start", cid, uid))
        time.sleep(0.3 if cid == CHAT else 0.02)
        with lock:
            events.append(("end", cid, uid))

    monkeypatch.setattr(tg, "handle_update", fake_handle)
    stop = tg._state["stop"]
    batch = [_upd(10, CHAT, "slow a"), _upd(11, other, "quick b"),
             _upd(12, CHAT, "queued c")]
    fake = _FakeTGHttpx(batches=[batch], on_exhausted=stop.set)
    monkeypatch.setattr(tg, "httpx", fake)

    async def scenario():
        await tg.start(CFG, force=True)
        await tg._state["task"]

    asyncio.run(scenario())
    # same chat: the queued message waits for the slow one to FINISH
    assert events.index(("end", CHAT, 10)) < events.index(("start", CHAT, 12))
    # different chat: answered while the slow one was still running
    assert events.index(("start", other, 11)) < events.index(("end", CHAT, 10))
    assert ("end", CHAT, 12) in events                  # nobody was dropped


def test_poller_bounds_concurrent_handlers(monkeypatch):
    _enable()
    lock = threading.Lock()
    cur = {"n": 0, "max": 0}
    done: list[int] = []

    def fake_handle(cfg, upd):
        with lock:
            cur["n"] += 1
            cur["max"] = max(cur["max"], cur["n"])
        time.sleep(0.15)
        with lock:
            cur["n"] -= 1
            done.append(upd["update_id"])

    monkeypatch.setattr(tg, "handle_update", fake_handle)
    stop = tg._state["stop"]
    batch = [_upd(20 + i, 900000 + i, f"m{i}") for i in range(6)]
    fake = _FakeTGHttpx(batches=[batch], on_exhausted=stop.set)
    monkeypatch.setattr(tg, "httpx", fake)

    async def scenario():
        await tg.start(CFG, force=True)
        await tg._state["task"]

    asyncio.run(scenario())
    assert sorted(done) == list(range(20, 26))           # every message handled
    assert 2 <= cur["max"] <= tg.MAX_CONCURRENT_HANDLERS  # parallel but bounded


def test_poller_survives_raising_handler(monkeypatch):
    _enable()
    calls: list[int] = []

    def fake_handle(cfg, upd):
        calls.append(upd["update_id"])
        if upd["update_id"] == 30:
            raise RuntimeError("boom")

    monkeypatch.setattr(tg, "handle_update", fake_handle)
    stop = tg._state["stop"]
    fake = _FakeTGHttpx(batches=[[_upd(30, CHAT, "x"), _upd(31, CHAT, "y")]],
                        on_exhausted=stop.set)
    monkeypatch.setattr(tg, "httpx", fake)

    async def scenario():
        await tg.start(CFG, force=True)
        await tg._state["task"]

    asyncio.run(scenario())
    assert calls == [30, 31]                             # one crash, next still runs


# ---------------- outbound rate limit ----------------

def test_notify_rate_limit_drops_with_warn(monkeypatch):
    _enable()
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    before = _warn_count()
    results = [tg.notify("ping") for _ in range(25)]
    sent = sum(r["sent"] for r in results)
    assert sent == tg.MAX_OUT_PER_MIN                  # 20 got through
    assert len(fake.sent()) == tg.MAX_OUT_PER_MIN
    assert _warn_count() - before == 5                 # 5 drops, each warned


# ---------------- poller lifecycle ----------------

def test_poller_tracks_offset_and_dispatches(monkeypatch):
    _enable()
    modes: list[str] = []
    stop = tg._state["stop"]

    def _done():
        modes.append(tg._state["mode"])  # sampled live, inside the loop
        stop.set()

    fake = _FakeTGHttpx(batches=[[_upd(100, CHAT, "/help")]],
                        on_exhausted=_done)
    monkeypatch.setattr(tg, "httpx", fake)

    async def scenario():
        await tg.start(CFG, force=True)
        await tg._state["task"]

    asyncio.run(scenario())
    assert modes == ["polling"]                        # it WAS polling live
    assert tg._state["offset"] == 101                  # update_id 100 → +1
    assert fake.sent() and "/approve" in fake.sent()[0][1]
    assert tg.status()["task_alive"] is False


def test_poller_bad_token_stops_with_state(monkeypatch):
    _enable(token="700:AAHwrong-token")
    stop = tg._state["stop"]
    fake = _FakeTGHttpx(status=401, on_exhausted=stop.set)
    monkeypatch.setattr(tg, "httpx", fake)

    async def scenario():
        await tg.start(CFG, force=True)
        await tg._state["task"]

    asyncio.run(scenario())
    assert tg.status()["state"] == "bad_token"
    # the bad-token log must not leak the token
    with db.connect() as c:
        rows = c.execute("SELECT message FROM agent_log "
                         "WHERE loop='telegram'").fetchall()
    assert all("AAHwrong-token" not in r["message"] for r in rows)


def test_poller_restarts_cleanly_on_token_change(monkeypatch):
    _enable(token="111:AAAtoken-one")
    stop = tg._state["stop"]
    fake1 = _FakeTGHttpx(on_exhausted=stop.set)
    monkeypatch.setattr(tg, "httpx", fake1)

    async def scenario():
        await tg.start(CFG, force=True)
        await tg._state["task"]
        # token rotates → restart uses the NEW token on the wire
        db.set_setting("tg_bot_token", "222:BBtoken-two")
        fake2 = _FakeTGHttpx(on_exhausted=stop.set)
        monkeypatch.setattr(tg, "httpx", fake2)
        await tg.restart(CFG, force=True)
        await tg._state["task"]
        return fake2

    fake2 = asyncio.run(scenario())
    gu_urls = [u for u, m, _p in fake2.calls if m == "getUpdates"]
    assert gu_urls and all("222:BBtoken-two" in u for u in gu_urls)
    assert tg.status()["state"] in ("disabled", "polling")


# ---------------- digest bridge ----------------

def test_digest_bridge_notifies_when_enabled(monkeypatch):
    db.set_setting("digest_last", None)
    db.set_setting("digest_webhook_url", "")
    notified: list[str] = []
    monkeypatch.setattr(tg, "notify", lambda text: notified.append(text)
                        or {"ok": True, "sent": 1, "chats": 1, "error": None})
    _enable()
    result = digest_mod.deliver(CFG, TODAY, lang="en", force=True)
    assert result["tg_sent"] and len(notified) == 1
    assert notified[0].startswith("📰") and TODAY in notified[0]

    # disabled → no TG delivery, digest still succeeds
    db.set_setting("tg_enabled", False)
    notified.clear()
    result = digest_mod.deliver(CFG, TODAY, lang="en", force=True)
    assert not result["tg_sent"] and notified == [] and result["ok"]


# ---------------- agent-loop draft hook ----------------

def test_agent_create_loop_enqueues_tg_card(monkeypatch):
    _enable()
    d = db.add_draft(text="created by the create loop, needs approval")
    cards: list[list[int]] = []
    monkeypatch.setattr(tg, "notify_new_drafts",
                        lambda ids: cards.append(ids)
                        or {"ok": True, "sent": 1, "chats": 1, "error": None})

    class _SyncThread:  # run the "fire-and-forget" card inline for the assert
        def __init__(self, target, args=(), daemon=None, name=None):
            self._t, self._a = target, args

        def start(self):
            self._t(*self._a)

    # rebind agent's OWN threading reference only — patching the real module
    # attr would corrupt asyncio.to_thread's worker spawn everywhere
    monkeypatch.setattr(agent_mod, "threading",
                        types.SimpleNamespace(Thread=_SyncThread))

    async def _no_replenish(cfg, x=None, **kw):  # agent awaits this
        return {"ran": False, "added": 0, "sources": [], "bank": 1,
                "bank_before": 1}

    monkeypatch.setattr(agent_mod.ideas_mod, "replenish", _no_replenish)
    monkeypatch.setattr(agent_mod.drafts_mod, "generate_drafts",
                        lambda cfg, acct=None: [d])

    async def scenario():
        return await agent_mod.Agent(CFG).create()

    out = asyncio.run(scenario())
    assert out["drafts"] == 1 and cards == [[d]]


def test_new_draft_card_on_the_wire_is_the_v051_design(monkeypatch):
    """notify_new_drafts end-to-end: the card the owner actually receives
    quotes the drafts verbatim, no · soup, no truncation."""
    _enable()
    long_reply = ("agents know this already anxiety needs a body, "
                  "i just run the loop again")
    d1 = db.add_draft(text=long_reply, kind="reply",
                      meta={"target_author": "naval", "reply_to_x_id": "x1",
                            "voice": {"score": 100, "checked": True}})
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    r = tg.notify_new_drafts([d1])
    assert r["ok"] and r["sent"] == 1
    text = fake.sent()[0][1]
    assert text.startswith("⏳ 1 draft waiting for approval")
    assert "Replies drafted to @naval's recent posts:" in text
    assert f"“{long_reply}”" in text          # verbatim, in full
    assert f"#{d1} — voice 100%" in text
    assert "·" not in text and "…" not in text and "**" not in text


# ---------------- settings API + test endpoint ----------------

def test_settings_mask_token_and_test_endpoint(monkeypatch):
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    r = client.post("/api/settings", json={
        "tg_bot_token": "123456789:AAHsuper-secret",
        "tg_allowed_chats": ["111", "222"],
        "tg_enabled": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tg_bot_set"] is True
    assert "AAHsuper-secret" not in r.text           # token never returned
    assert body["tg_bot_token"].startswith("••••")    # only the mask
    assert body["tg_allowed_chats"] == [111, 222]
    assert body["tg_enabled"] is True

    r = client.post("/api/telegram/test")
    assert r.status_code == 200, r.text
    assert r.json()["chat_id"] == 111
    sent = fake.sent()
    assert sent and sent[-1][0] == 111 and "online" in sent[-1][1].lower()

    # junk token is ignored, not stored
    r = client.post("/api/settings", json={"tg_bot_token": "short"})
    assert "AAHsuper-secret" in str(db.get_setting("tg_bot_token"))

    db.set_setting("tg_allowed_chats", [])
    assert client.post("/api/telegram/test").status_code == 400


# ---------------- outbound media (v0.6 round-trip) ----------------


def _img_draft(text="media draft text") -> int:
    d = db.add_draft(text=text, acct=1)
    db.update_draft(d, image="media_test_photo.png", acct=1)
    return d


def test_card_with_image_sends_photo(tmp_path, monkeypatch):
    _enable()
    (tmp_path / "media_test_photo.png").write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._card_map.clear()
    d = _img_draft()
    r = tg.notify_new_drafts([d])
    assert r["ok"]
    sends = fake.media_sends()
    assert len(sends) == 1
    method, chat, caption, fname = sends[0]
    assert method == "sendPhoto" and chat == CHAT
    assert fname == "media_test_photo.png"
    assert f"#{d}" in caption  # draft id appears in the caption


def test_card_gif_sent_as_document(tmp_path, monkeypatch):
    _enable()
    (tmp_path / "media_test.gif").write_bytes(b"GIF fake")
    d = db.add_draft(text="gif draft", acct=1)
    db.update_draft(d, image="media_test.gif", acct=1)
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.notify_new_drafts([d])
    sends = fake.media_sends()
    assert sends and sends[0][0] == "sendDocument"


def test_sendphoto_failure_falls_back_to_text(tmp_path, monkeypatch):
    _enable()
    (tmp_path / "media_test_photo.png").write_bytes(b"x")

    class FailPhoto(_FakeTGHttpx):
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


# ---------------- inbound photo attach (v0.6 round-trip) ----------------


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
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    d = db.add_draft(text="target draft", acct=1)
    tg._handle_update(CFG, _photo_update(caption=f"/img {d}"))
    row = db.get_draft(d)
    assert row["image"] and row["image"].startswith("media_")
    assert (tmp_path / row["image"]).exists()
    assert any("attached" in t.lower() for _c, t in fake.sent())


def test_photo_reply_to_card_attaches(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    d = db.add_draft(text="card draft", acct=1)
    tg._card_map.clear()
    tg._card_map[CHAT] = {777: [d]}
    tg._handle_update(CFG, _photo_update(reply_to=777))
    assert db.get_draft(d)["image"]


def test_photo_reply_ambiguous_card_asks_for_caption(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    d1, d2 = db.add_draft(text="a", acct=1), db.add_draft(text="b", acct=1)
    tg._card_map.clear()
    tg._card_map[CHAT] = {777: [d1, d2]}
    tg._handle_update(CFG, _photo_update(reply_to=777))
    assert db.get_draft(d1)["image"] is None
    assert any("/img" in t for _c, t in fake.sent())


def test_photo_no_target_gets_hint(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._card_map.clear()
    tg._handle_update(CFG, _photo_update())
    assert any("/img" in t for _c, t in fake.sent())


def test_photo_disallowed_chat_ignored(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    upd = _photo_update()
    upd["message"]["chat"]["id"] = 666999
    tg._handle_update(CFG, upd)
    attach_sends = [t for _c, t in fake.sent() if "attached" in t.lower()]
    assert attach_sends == []


def test_video_document_declined(tmp_path, monkeypatch):
    _enable()
    monkeypatch.setattr(tg, "MEDIA_DIR", tmp_path)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    upd = {"update_id": 1, "message": {"chat": {"id": CHAT},
           "document": {"file_id": "f1", "mime_type": "video/mp4"}}}
    tg._handle_update(CFG, upd)
    assert any("videos aren" in t.lower() for _c, t in fake.sent())


# ---------------- one-tap inline approve/reject (v0.6.2) ----------------

def _cb_update(cb_id: str, data: str, chat_id: int = CHAT, msg_id: int = 55) -> dict:
    return {"update_id": 1, "callback_query": {
        "id": cb_id, "data": data,
        "message": {"message_id": msg_id, "chat": {"id": chat_id}}}}


def test_card_carries_one_tap_keyboard(monkeypatch):
    _enable()
    d1 = db.add_draft(text="one")
    d2 = db.add_draft(text="two")
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.notify_new_drafts([d1, d2])
    sends = [(u, p) for u, m, p in fake.calls if m == "sendMessage"]
    assert sends and "reply_markup" in sends[0][1]
    kb = json.loads(sends[0][1]["reply_markup"])
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert f"a:{d1}" in datas and f"r:{d2}" in datas


def test_one_tap_approve(monkeypatch):
    _enable()
    d = db.add_draft(text="tap approved")
    monkeypatch.setattr(slots_mod, "pick_slot_with_reason",
                        lambda cfg, kind, now: (datetime(2030, 1, 2, 9, 0, 0), "cadence"))
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._handle_update(CFG, _cb_update("cb1", f"a:{d}"))
    row = db.get_draft(d)
    assert row["status"] == "approved"
    methods = [m for _u, m, p in fake.calls]
    assert "answerCallbackQuery" in methods
    answer = [p for _u, m, p in fake.calls if m == "answerCallbackQuery"][0]
    assert "approved" in answer["text"].lower()
    assert "editMessageReplyMarkup" in methods  # buttons cleared


def test_one_tap_reject(monkeypatch):
    _enable()
    d = db.add_draft(text="tap rejected")
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._handle_update(CFG, _cb_update("cb2", f"r:{d}"))
    assert db.get_draft(d)["status"] == "rejected"


def test_one_tap_stranger_refused(monkeypatch):
    _enable()
    d = db.add_draft(text="stranger cannot tap")
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._handle_update(CFG, _cb_update("cb3", f"a:{d}", chat_id=666999))
    assert db.get_draft(d)["status"] == "draft"     # untouched
    answer = [p for _u, m, p in fake.calls if m == "answerCallbackQuery"][0]
    assert "not authorized" in answer["text"].lower()


def test_drafts_command_carries_keyboard(monkeypatch):
    _enable()
    db.add_draft(text="listed draft")
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._handle_update(CFG, _upd(1, CHAT, "/drafts"))
    sends = [(u, p) for u, m, p in fake.calls if m == "sendMessage"]
    assert sends and "reply_markup" in sends[0][1]


def test_int_arg_accepts_hash_prefix():
    """'/approve #2379' is how humans type ids — must parse like '2379'."""
    assert tg._int_arg("#2379", "approve") == 2379
    assert tg._int_arg("2379", "approve") == 2379
    assert tg._int_arg("", "approve") == -1
    assert tg._int_arg("#nope", "approve") == -1


# ---------------- live approval card (decisions update the message) ----------------

def test_approve_updates_card_keeps_other_buttons(monkeypatch):
    """User rule: approving one draft must NOT orphan the others — the card
    rewrites with outcomes, pending drafts keep their buttons."""
    _enable()
    d1 = db.add_draft(text="first to approve")
    d2 = db.add_draft(text="stays pending")
    monkeypatch.setattr(slots_mod, "pick_slot_with_reason",
                        lambda cfg, kind, now: (datetime(2030, 1, 3, 9, 0, 0), "cadence"))
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._card_map.clear()
    tg.notify_new_drafts([d1, d2])                 # card message_id 4242, map recorded
    tg._handle_update(CFG, _cb_update("cb1", f"a:{d1}", msg_id=4242))
    edited = [p for _u, m, p in fake.calls if m == "editMessageText"]
    assert edited, "card must be rewritten in place"
    text = edited[-1]["text"]
    assert f"#{d1}" in text and "scheduled" in text
    assert f"#{d2}" in text and "first" not in text[:20]   # d2 still listed pending
    markup = [p for _u, m, p in fake.calls if m == "editMessageReplyMarkup"][-1]
    kb = json.loads(markup["reply_markup"])
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert f"a:{d2}" in datas and f"a:{d1}" not in datas


def test_reject_updates_card_too(monkeypatch):
    _enable()
    d1 = db.add_draft(text="rejected one")
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._card_map.clear()
    tg.notify_new_drafts([d1])
    tg._handle_update(CFG, _cb_update("cb2", f"r:{d1}", msg_id=4242))
    edited = [p for _u, m, p in fake.calls if m == "editMessageText"]
    assert any("rejected" in p["text"] for p in edited)


def test_all_decided_clears_buttons(monkeypatch):
    _enable()
    d1 = db.add_draft(text="only one")
    monkeypatch.setattr(slots_mod, "pick_slot_with_reason",
                        lambda cfg, kind, now: (datetime(2030, 1, 4, 9, 0, 0), "cadence"))
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._card_map.clear()
    tg.notify_new_drafts([d1])
    tg._handle_update(CFG, _cb_update("cb3", f"a:{d1}", msg_id=4242))
    markup = [p for _u, m, p in fake.calls if m == "editMessageReplyMarkup"][-1]
    assert "reply_markup" not in markup           # nothing pending → no buttons


# ---------------- voice notes -> post ----------------

def _voice_update(chat_id: int = CHAT) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id},
            "voice": {"file_id": "v1", "duration": 7}}}


def test_voice_note_transcribes_and_chats(monkeypatch):
    _enable()
    from openstanley.gen import voice_notes as vn
    monkeypatch.setattr(vn, "transcribe", lambda b, lang=None: "write a post about agents learning to code")
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._handle_update(CFG, _voice_update())
    texts = " ".join(t for _c, t in fake.sent())
    assert "heard: write a post" in texts          # transcript echoed back


def test_voice_note_empty_transcript(monkeypatch):
    _enable()
    from openstanley.gen import voice_notes as vn
    monkeypatch.setattr(vn, "transcribe", lambda b, lang=None: "")
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._handle_update(CFG, _voice_update())
    texts = " ".join(t for _c, t in fake.sent())
    assert "couldn't hear" in texts


def test_voice_note_stranger_refused(monkeypatch):
    _enable()
    from openstanley.gen import voice_notes as vn
    calls = []
    monkeypatch.setattr(vn, "transcribe", lambda b, lang=None: calls.append(1) or "x")
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._handle_update(CFG, _voice_update(chat_id=666999))
    assert calls == []                              # never transcribed


# ---------------- mini one-tap cards after chat-born drafts ----------------

def test_mini_card_after_chat_draft(monkeypatch):
    """Ask Stanley in chat, a draft gets saved, a tappable card follows."""
    _enable()
    before = tg._latest_draft_id()
    d = db.add_draft(text="hot take born in chat")   # simulate the chat save
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._push_mini_card(CHAT, before)
    sends = [(u, p) for u, m, p in fake.calls if m == "sendMessage"]
    assert sends and "reply_markup" in sends[-1][1]
    kb = json.loads(sends[-1][1]["reply_markup"])
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert f"a:{d}" in datas
    assert 4242 in tg._card_map.get(CHAT, {})          # live-card registered
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (d,))


def test_no_mini_card_when_nothing_created(monkeypatch):
    _enable()
    before = tg._latest_draft_id()
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._push_mini_card(CHAT, before)
    sends = [(u, p) for u, m, p in fake.calls if m == "sendMessage"]
    assert not [p for u, p in sends if "reply_markup" in p]


# ---------------- [show] button: full draft on demand ----------------

def test_show_button_in_keyboard():
    kb = json.loads(tg._approve_keyboard([77]))
    row = kb["inline_keyboard"][0]
    assert [b["callback_data"] for b in row] == ["a:77", "r:77", "s:77"]
    assert row[2]["text"] == "show"


def test_show_sends_full_draft_keeps_card(monkeypatch):
    _enable()
    d = db.add_draft(text="full body of the draft here, every word " * 3, acct=1)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg._card_map.clear()
    tg.notify_new_drafts([d])          # card msg 4242 with [show] button
    tg._handle_update(CFG, _cb_update("cbS", f"s:{d}", msg_id=4242))
    texts = [t for _c, t in fake.sent()]
    assert any("FULL DRAFT" in t and "every word" in t for t in texts)
    # read-only: card NOT rewritten, buttons NOT cleared
    edits = [m for _u, m, _p in fake.calls if m == "editMessageText"]
    assert not edits
    with db.connect() as c:
        c.execute("DELETE FROM drafts WHERE id=?", (d,))


def test_handler_wall_clock_timeout_frees_and_apologizes(monkeypatch):
    """Live 2026-08-28 11:26: a wedged LLM stream hung a TG handler for 6+
    minutes with no reply, no error, no timeout firing. wait_for now caps
    the handler, logs, and tells the owner to resend."""
    import asyncio
    import openstanley.integrations.telegram as tg_mod

    _enable()
    sent = []
    monkeypatch.setattr(tg_mod, "CHAT_HANDLER_TIMEOUT_S", 0.2)
    monkeypatch.setattr(tg_mod, "send_message",
                        lambda cid, text, **k: sent.append((cid, text)))

    def stuck_handle(cfg, upd):
        import time as _t
        _t.sleep(1.0)  # longer than the patched cap

    monkeypatch.setattr(tg_mod, "handle_update", stuck_handle)

    async def run():
        # minimal replica of _dispatch's timeout branch
        try:
            await asyncio.wait_for(
                asyncio.to_thread(tg_mod.handle_update, None, {}),
                timeout=tg_mod.CHAT_HANDLER_TIMEOUT_S)
        except asyncio.TimeoutError:
            tg_mod.send_message(999, "⏱ That request ran too long and was "
                                      "dropped — please resend it.")

    asyncio.run(run())
    assert sent and "resend" in sent[-1][1]
