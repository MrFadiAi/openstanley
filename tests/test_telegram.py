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


class _FakeTGHttpx:
    """Records Bot API calls; serves scripted getUpdates batches, then empty.
    on_exhausted fires when the batches run dry (tests stop the loop there)."""

    def __init__(self, batches: list | None = None, status: int = 200,
                 on_exhausted=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.batches = list(batches or [])
        self.status = status
        self.on_exhausted = on_exhausted

    def post(self, url, json=None, timeout=None, **kw):  # noqa: A002
        method = url.rsplit("/", 1)[-1]
        self.calls.append((url, method, dict(json or {})))
        if method == "getUpdates":
            if self.status != 200:  # e.g. 401 → the bad-token path
                return _R(self.status, {"ok": False})
            if self.batches:
                return _R(200, {"ok": True, "result": self.batches.pop(0)})
            if self.on_exhausted:
                self.on_exhausted()
            return _R(200, {"ok": True, "result": []})
        return _R(self.status, {"ok": True})

    def sent(self) -> list[tuple[int, str]]:
        """(chat_id, text) of every sendMessage the fake saw."""
        return [(p.get("chat_id"), p.get("text", ""))
                for _u, m, p in self.calls if m == "sendMessage"]


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
    monkeypatch.setattr(brain, "BRAIN_DIR", sandbox)
    monkeypatch.setattr(brain, "FILES_DIR", sandbox / "files")
    monkeypatch.setattr(brain, "PHOTOS_DIR", sandbox / "photos")
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
    assert f"idea bank: {bank}" in text
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
    assert "15+" in fake.sent()[1][1]


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


def test_chat_candidates_saved_as_drafts(monkeypatch):
    """Defect 1 — a post drafted in TG chat becomes a real draft the user can
    /approve. Same draft_from_chat path as the web UI's approval cards."""
    _enable()
    post = "an ugly first version teaches what slides never will"
    monkeypatch.setattr(chat_mod, "llm_chat_stream",
                        lambda *a, **k: iter([f"here you go:\n> {post}\n"]))
    monkeypatch.setattr(chat_mod, "llm_chat", lambda *a, **k: "")
    _stub_voice(monkeypatch)
    tg._sessions.clear()
    out = "".join(tg.chat_reply_tg_stream(CFG, CHAT, "draft me a post"))
    m = re.search(r"saved as draft #(\d+) — /approve \1 to publish", out)
    assert m, out
    d = db.get_draft(int(m.group(1)))
    assert d["text"] == post
    assert d["status"] == "draft"                       # queued, NOT published
    assert d["meta"]["source"] == "chat"                # web-UI draft path
    assert out.count(post) == 1                         # prose never duplicated
    assert tg._sessions[CHAT][-1]["content"].endswith(
        f"saved as draft #{m.group(1)} — /approve {m.group(1)} to publish")


def test_chat_multiple_candidates_each_saved(monkeypatch):
    _enable()
    p1 = "first candidate post, long enough to count"
    p2 = "second candidate post, also long enough"
    monkeypatch.setattr(chat_mod, "llm_chat_stream",
                        lambda *a, **k: iter([f"> {p1}\n\n> {p2}\n"]))
    monkeypatch.setattr(chat_mod, "llm_chat", lambda *a, **k: "")
    _stub_voice(monkeypatch)
    tg._sessions.clear()
    out = "".join(tg.chat_reply_tg_stream(CFG, CHAT, "draft two posts"))
    ids = re.findall(r"saved as draft #(\d+)", out)
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
                        lambda cfg: [d])

    async def scenario():
        return await agent_mod.Agent(CFG).create()

    out = asyncio.run(scenario())
    assert out["drafts"] == 1 and cards == [[d]]


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
