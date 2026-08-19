"""Telegram /study (v0.4.4.1) — the full learn-me chain from chat.

Per TEST_BRIEF_STUDY.md: routing variants + HELP_TEXT + unknown commands
never reaching the chain; the auth gate refusing strangers BEFORE the chain
runs; _cmd_study directly (four loops in order import → study → scan → learn,
summary lines carrying REAL numbers from the loop results, the @handle chip,
a failing loop surfacing its error instead of dropping the line); the
handler-level update path incl. through the poller's to_thread worker;
loop-runner reuse (importing the server from the telegram module: no
circular import, no port bind, no scheduler start); and the per-loop timeout
that keeps a hung loop from parking the poller forever.

All hermetic: agent loops faked on the server's live agent, Bot API traffic
faked at the module httpx seam, dryrun X, OPENSTANLEY_TEST_DB.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"  # before importing the server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from openstanley.core import db               # noqa: E402
db.init_db()

import openstanley.server.__main__ as server  # noqa: E402
from openstanley.core.config import Config    # noqa: E402
from openstanley.integrations import telegram as tg  # noqa: E402

CHAT = 111222333
CFG = Config()

# realistic agent-loop results — the exact shapes openstanley.gen.agent returns
RESULTS = {
    "import": {"me": {"username": "fadi", "followers": 1234}, "own": 12, "niche": 30},
    "study": {"niche_new": 3, "bank": 18, "replenished": 0},
    "scan": {"posts_scanned": 800, "languages": {"en": 1.0}, "voice": "rebuilt",
             "brain": "brain: +1 rules, 0 strategies"},
    "learn": {"refreshed": 60, "metrics": "60 posts captured, followers=1234",
              "voice": "rebuilt", "brain": "brain: +2 rules, 1 strategies"},
}

_METHODS = {"import": "import_history", "study": "study",
            "scan": "scan", "learn": "learn"}


# ---------------- helpers ----------------

def _upd(update_id: int, chat_id: int, text: str) -> dict:
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text,
                        "from": {"id": chat_id}}}


class _R:
    def __init__(self, payload: dict):
        self.status_code = 200
        self.text = "ok"
        self._p = payload

    def json(self) -> dict:
        return self._p


class _FakeTGHttpx:
    """Records Bot API calls; serves scripted getUpdates batches, then empty.
    on_exhausted fires when the batches run dry (poller tests stop there)."""

    def __init__(self, batches: list | None = None, on_exhausted=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.batches = list(batches or [])
        self.on_exhausted = on_exhausted

    def post(self, url, json=None, timeout=None, **kw):  # noqa: A002
        method = url.rsplit("/", 1)[-1]
        self.calls.append((url, method, dict(json or {})))
        if method == "getUpdates":
            if self.batches:
                return _R({"ok": True, "result": self.batches.pop(0)})
            if self.on_exhausted:
                self.on_exhausted()
            return _R({"ok": True, "result": []})
        return _R({"ok": True})

    def sent(self) -> list[tuple[int, str]]:
        return [(p.get("chat_id"), p.get("text", "")) for _u, m, p in self.calls
                if m == "sendMessage"]


def _enable(chats: list[int] | None = None) -> None:
    db.set_setting("tg_bot_token", "700:AAHtest-token")
    db.set_setting("tg_allowed_chats", [CHAT] if chats is None else chats)
    db.set_setting("tg_enabled", True)


@pytest.fixture(autouse=True)
def _tg_sandbox():
    """Fresh poller/session/rate state; telegram settings restored after."""
    tg._reset_rate()
    tg._denied_chats.clear()
    tg._sessions.clear()
    tg._state.update(task=None, offset=0, mode="disabled")
    tg._state["stop"].clear()
    yield
    db.set_setting("tg_bot_token", "")
    db.set_setting("tg_allowed_chats", [])
    db.set_setting("tg_enabled", False)


def _fake_loops(monkeypatch, calls: list[str], fail=(), hang=()) -> None:
    """Patch the four chain methods on the server's live agent (instance
    attrs, undone by monkeypatch). Records call order in `calls`; names in
    `fail` raise, names in `hang` park (to exercise the timeout bound)."""
    def make(name: str):
        async def _fake():
            calls.append(name)
            if name in hang:
                await asyncio.sleep(5)
            if name in fail:
                raise RuntimeError(f"{name} exploded")
            return RESULTS[name]
        return _fake
    for name, method in _METHODS.items():
        monkeypatch.setattr(server.agent, method, make(name))


# ---------------- 1. routing & surface ----------------

def test_study_parses_across_variants():
    assert tg.parse_command("/study") == ("study", "")
    assert tg.parse_command("/study@my_bot") == ("study", "")
    assert tg.parse_command("/STUDY") == ("study", "")
    assert tg.parse_command("/Study  now please") == ("study", "now please")


def test_help_text_lists_study():
    assert "/study" in tg.HELP_TEXT


def test_unknown_command_never_triggers_the_chain(monkeypatch):
    _enable()
    calls: list[str] = []
    _fake_loops(monkeypatch, calls)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, CHAT, "/frobnicate"))
    sent = fake.sent()
    assert len(sent) == 1
    assert "Unknown command" in sent[0][1]
    assert calls == []                      # the chain never ran


# ---------------- auth gate ----------------

def test_disallowed_chat_refused_and_chain_never_runs(monkeypatch):
    _enable()
    calls: list[str] = []
    _fake_loops(monkeypatch, calls)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(1, 999999, "/study"))
    sent = fake.sent()
    assert len(sent) == 1 and "private" in sent[0][1].lower()
    assert calls == []                      # refused BEFORE the chain ran


# ---------------- 2. unit: _cmd_study ----------------

def test_cmd_study_runs_four_loops_in_order_with_real_numbers(monkeypatch):
    calls: list[str] = []
    _fake_loops(monkeypatch, calls)
    reply = tg._cmd_study(CFG)
    assert calls == ["import", "study", "scan", "learn"]    # the order
    lines = reply.splitlines()
    assert lines[0].startswith("· **import**")
    assert "12 own + 30 niche" in lines[0]                  # real numbers
    assert "· @fadi (1234 followers)" in lines[0]           # me chip renders
    assert any(l.startswith("· **study** +3 niche · bank 18") for l in lines)
    assert any(l.startswith("· **scan** 800 posts · voice rebuilt") for l in lines)
    assert any(l.startswith("· **learn** refreshed 60 posts") for l in lines)
    assert "✅" in reply


def test_cmd_study_failing_scan_surfaces_error_and_continues(monkeypatch):
    calls: list[str] = []
    _fake_loops(monkeypatch, calls, fail={"scan"})
    reply = tg._cmd_study(CFG)
    assert calls == ["import", "study", "scan", "learn"]    # chain moved on
    assert "scan exploded" in reply                         # error surfaced
    assert "· **scan** failed — scan exploded" in reply
    assert "· **import** 12 own" in reply                   # earlier lines kept
    assert "· **learn** refreshed 60 posts" in reply        # later loop ran
    assert "✅" not in reply and "⚠️" in reply
    # the failure was logged through the shared core, like the dashboard path
    with db.connect() as c:
        (n,) = c.execute(
            "SELECT COUNT(*) FROM agent_log WHERE loop='api' AND level='error' "
            "AND message LIKE '%scan exploded%'").fetchone()
    assert int(n) >= 1


def test_hung_loop_times_out_and_chain_finishes(monkeypatch):
    """A loop that never returns must not park the poller thread forever —
    the per-loop timeout bounds it, the line says so, the rest still runs."""
    calls: list[str] = []
    _fake_loops(monkeypatch, calls, hang={"import"})
    monkeypatch.setattr(tg, "STUDY_LOOP_TIMEOUT_S", 0.05)
    reply = tg._cmd_study(CFG)
    assert "timed out" in reply
    assert "· **import** timed out" in reply                # its line marks it
    assert "· **learn** refreshed 60 posts" in reply        # the rest still ran
    assert "✅" not in reply and "⚠️" in reply


# ---------------- 3. handler-level: simulated Telegram update ----------------

def test_study_update_replies_once_with_summary(monkeypatch):
    _enable()
    calls: list[str] = []
    _fake_loops(monkeypatch, calls)
    fake = _FakeTGHttpx()
    monkeypatch.setattr(tg, "httpx", fake)
    tg.handle_update(CFG, _upd(7, CHAT, "/study"))
    sent = fake.sent()
    assert len(sent) == 1                                   # exactly one reply
    assert sent[0][0] == CHAT
    assert sent[0][1].startswith("· <b>import</b>")         # bold on the wire
    assert "✅" in sent[0][1]
    assert calls == ["import", "study", "scan", "learn"]


def test_study_runs_inside_the_pollers_worker_thread(monkeypatch):
    """The chain is synchronous in the handler — the poller runs it via
    asyncio.to_thread; prove the whole path works from long-poll to reply."""
    _enable()
    calls: list[str] = []
    _fake_loops(monkeypatch, calls)
    stop = tg._state["stop"]
    fake = _FakeTGHttpx(batches=[[_upd(50, CHAT, "/study@openstanley_bot")]],
                        on_exhausted=stop.set)
    monkeypatch.setattr(tg, "httpx", fake)

    async def scenario():
        await tg.start(CFG, force=True)
        await tg._state["task"]

    asyncio.run(scenario())
    assert calls == ["import", "study", "scan", "learn"]    # ran in the worker
    sent = fake.sent()
    assert len(sent) == 1 and "✅" in sent[0][1]
    assert sent[0][1].startswith("· <b>import</b>")         # bold on the wire
    assert tg._state["offset"] == 51


# ---------------- 4. loop-runner reuse ----------------

_SUBPROC_CHECK = """
import importlib, sys
root = sys.argv[1]
sys.path.insert(0, root)
from openstanley.integrations import telegram as tg
srv = importlib.import_module("openstanley.server.__main__")  # what _cmd_study imports lazily
assert hasattr(srv, "run_loop_core"), "server must expose the shared loop core"
assert srv.scheduler is None, "import must not start the scheduler"
assert "uvicorn" not in sys.modules, "import must not boot a server"
assert "apscheduler" not in sys.modules, "no scheduler machinery on import"
assert callable(tg._cmd_study)
print("SUBPROCESS_OK")
"""


def test_importing_server_from_telegram_is_side_effect_free(tmp_path):
    """Fresh process: telegram first, then the server module exactly the way
    _cmd_study pulls it in lazily — no circular import, no port bind, no
    scheduler start."""
    env = dict(os.environ)
    env.update(OPENSTANLEY_X_MODE="dryrun",
               OPENSTANLEY_NO_SCHEDULER="1", OPENSTANLEY_NO_TELEGRAM="1",
               OPENSTANLEY_NO_SMOKE="1",
               OPENSTANLEY_TEST_DB=str(tmp_path / "sub.db"),
               OPENSTANLEY_DIGEST_DIR=str(tmp_path / "digests"))
    for k in ("OPENSTANLEY_X_COOKIES", "OPENSTANLEY_X_BEARER", "OPENSTANLEY_X_API_KEY"):
        env.pop(k, None)
    r = subprocess.run([sys.executable, "-c", _SUBPROC_CHECK, str(ROOT)],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stderr
    assert "SUBPROCESS_OK" in r.stdout
