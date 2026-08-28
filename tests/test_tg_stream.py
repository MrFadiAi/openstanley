"""Hermetic streaming-chat tests — Bot API faked at the _api seam, zero network,
no real LLM (llm_chat_stream monkeypatched). Real DB untouched (test-db env)."""
import dataclasses
import sys, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from openstanley.integrations import telegram as tg
from openstanley.core.config import Config, LLMConfig


class FakeAPI:
    def __init__(self):
        self.calls = []  # (method, params)

    def __call__(self, token, method, params):
        self.calls.append((method, params))
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 42}},
            text="{}",
        )


@pytest.fixture()
def fake_api(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(tg, "_api", api)
    monkeypatch.setattr(tg, "bot_token", lambda: "123:FAKE")
    return api


def _cfg():
    llm = dataclasses.replace(LLMConfig(), model="m",
                              api_key_env="OPENSTANLEY_TEST_KEY",
                              base_url="u")
    return dataclasses.replace(Config(), llm=llm)


def _chunks(*parts):
    for p in parts:
        yield p


def test_send_stream_sends_then_edits_to_full(fake_api, monkeypatch):
    monkeypatch.setattr(tg, "STREAM_EDIT_MIN_S", 0.0)
    monkeypatch.setattr(tg, "STREAM_EDIT_EVERY", 1)
    parts = ["trained ", "a model ", "today.\n", "it ", "hallucinated ", "me."]
    r = tg.send_stream(1, _chunks(*parts))
    assert r["ok"] and r["message_id"] == 42
    methods = [m for m, _ in fake_api.calls]
    assert methods[0] == "sendMessage"
    assert "editMessageText" in methods
    assert methods[-1] == "editMessageText"
    assert fake_api.calls[-1][1]["text"] == "".join(parts)


def test_send_stream_short_reply_single_message(fake_api, monkeypatch):
    monkeypatch.setattr(tg, "STREAM_EDIT_MIN_S", 0.0)
    monkeypatch.setattr(tg, "STREAM_EDIT_EVERY", 1)
    r = tg.send_stream(1, _chunks("hi"))
    assert r["ok"]
    methods = [m for m, _ in fake_api.calls]
    assert methods == ["sendMessage"]


def test_send_stream_aborts_cleanly_on_error(fake_api):
    def exploding():
        yield "partial text delivered"
        raise RuntimeError("boom")
    r = tg.send_stream(1, exploding())
    # design: partial text IS delivered, never raises — ok even on abort
    assert r["error"] or r["ok"]
    delivered = [c for c in fake_api.calls if c[0] == "sendMessage"]
    assert delivered and "partial" in delivered[0][1]["text"]


def test_send_stream_no_token(monkeypatch):
    monkeypatch.setattr(tg, "bot_token", lambda: "")
    r = tg.send_stream(1, _chunks("x"))
    assert r["ok"] is False and r["error"] == "no bot token"


def test_streaming_chat_generator_yields_tokens(monkeypatch, fake_api):
    import openstanley.gen.chat as chat_mod
    monkeypatch.setattr(chat_mod, "llm_chat_stream",
                        lambda *a, **k: iter(["token1 ", "token2"]))
    monkeypatch.setattr(chat_mod, "_system", lambda c, m: "sys")
    tg._sessions.clear()
    out = list(tg.chat_reply_tg_stream(_cfg(), 99, "hello"))
    assert "".join(out).startswith("token1 token2")
    sess = tg._sessions.get(99)
    assert sess and sess[-1]["role"] == "assistant"


def test_handler_routes_chat_via_stream(monkeypatch, fake_api):
    used = {}
    def fake_send_stream(chat_id, stream):
        used["text"] = "".join(stream)
        return {"ok": True}
    monkeypatch.setattr(tg, "send_stream", fake_send_stream)
    import openstanley.gen.chat as chat_mod
    monkeypatch.setattr(chat_mod, "llm_chat_stream",
                        lambda *a, **k: iter(["streamed ", "answer"]))
    monkeypatch.setattr(chat_mod, "_system", lambda c, m: "sys")
    monkeypatch.setattr(tg, "_auth_reply", lambda cid: None)
    upd = {"message": {"chat": {"id": 7}, "text": "plain question"}}
    tg._handle_update(_cfg(), upd)
    assert used.get("text") == "streamed answer"


def test_chat_turn_uses_a_budget_thinking_cannot_starve(monkeypatch):
    """The whole 'agent not responding' day (2026-08-28) was ONE number:
    max_tokens=1200 — GLM's thinking phase consumed the entire budget
    before emitting text (stop_reason=max_tokens, zero deltas). Chat
    turns must budget for thinking + a full reply."""
    import dataclasses as _dc
    import openstanley.gen.chat as chat_mod
    seen = {}

    def fake_stream(cfg, system, user, temperature=None):
        seen["max_tokens"] = cfg.max_tokens
        yield "ok here is a reply"
    monkeypatch.setattr(chat_mod, "llm_chat_stream", fake_stream)
    monkeypatch.setattr(chat_mod, "llm_chat", lambda *a, **k: "")
    import openstanley.integrations.telegram as tgm
    monkeypatch.setattr(tgm, "send_stream",
                        lambda cid, stream: {"ok": True})
    tgm._sessions.clear()
    from openstanley.core.config import Config
    "".join(tgm.chat_reply_tg_stream(Config(), 123, "draft me a post"))
    assert seen.get("max_tokens", 0) >= 4000, seen
