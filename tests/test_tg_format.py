"""Telegram message quality (v0.4.5) — Hermes-grade styling.

FIX_BRIEF_TG_STYLE.md, two problems:

  1. TG chat replies used the DASHBOARD write-assistant system prompt, so the
     X-post voice (lowercase prose, quirk imitation) leaked into the
     conversation. The chat path must use the TG assistant persona — same
     brain, clean conversational voice; the X voice lives only inside quoted
     post candidates.

  2. Every sendMessage/editMessageText went out as plain text. Now: HTML
     parse_mode, a markdown→TG-HTML converter (escape first, unbalanced
     markers stay literal, clip BEFORE tagging so tags never get cut), and a
     one-shot plain-text retry when Telegram rejects the entities.

All hermetic: the Bot API is faked at the _api seam, every LLM seam is
monkeypatched (no real key traffic), the brain is sandboxed into tmp.
"""
from __future__ import annotations

import dataclasses
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from openstanley.core.config import Config, LLMConfig   # noqa: E402
from openstanley.integrations import telegram as tg     # noqa: E402


def _cfg() -> Config:
    llm = dataclasses.replace(LLMConfig(), model="m",
                              api_key_env="OPENSTANLEY_TEST_KEY",
                              base_url="u")
    return dataclasses.replace(Config(), llm=llm)


def _chunks(*parts):
    for p in parts:
        yield p


class FakeAPI:
    """Bot API at the _api seam — every call 200, params recorded."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, token, method, params):
        self.calls.append((method, dict(params)))
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 42}},
            text="{}",
        )

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


class ParseRejectAPI:
    """400 "can't parse entities" for EVERY formatted attempt, 200 for plain."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, token, method, params):
        self.calls.append((method, dict(params)))
        if params.get("parse_mode"):
            return types.SimpleNamespace(
                status_code=400,
                json=lambda: {"ok": False, "error_code": 400},
                text="Bad Request: can't parse entities: unclosed tag",
            )
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 7}},
            text="ok",
        )

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Brain in tmp, reflection inert, fresh TG state — the LLM never runs."""
    from openstanley.gen import brain
    monkeypatch.setattr(brain, "ACCOUNTS_ROOT", tmp_path / "accounts")
    brain.ensure()
    monkeypatch.setattr(brain, "maybe_reflect_chat_async", lambda cfg: False)
    tg._reset_rate()
    tg._denied_chats.clear()
    tg._sessions.clear()
    yield


@pytest.fixture()
def fake_api(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(tg, "_api", api)
    monkeypatch.setattr(tg, "bot_token", lambda: "123:FAKE")
    return api


# ---------------- converter: markdown → Telegram HTML ----------------

def test_converter_bold_italic_code():
    assert tg._md_to_tg_html("**bold** text") == "<b>bold</b> text"
    assert tg._md_to_tg_html("*italic* text") == "<i>italic</i> text"
    assert tg._md_to_tg_html("run `npm test` now") == "run <code>npm test</code> now"


def test_converter_escapes_stray_entities_before_tagging():
    assert tg._md_to_tg_html("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    # escaped FIRST — a bolded ampersand still renders correctly
    assert tg._md_to_tg_html("**&**") == "<b>&amp;</b>"


def test_converter_code_blocks_map_to_pre():
    out = tg._md_to_tg_html("```python\nprint(1)\n```")
    assert out == "<pre>print(1)\n</pre>"


def test_converter_leaves_code_contents_alone():
    # markers inside code are literal, not nested tags; angle brackets in
    # code are escaped but still RENDER as angle brackets
    assert tg._md_to_tg_html("`**not bold**`") == "<code>**not bold**</code>"
    assert (tg._md_to_tg_html("```\nprint('<b>')\n```")
            == "<pre>print('&lt;b&gt;')\n</pre>")


def test_converter_unbalanced_markers_stay_literal():
    assert tg._md_to_tg_html("**bold") == "**bold"              # no close
    assert tg._md_to_tg_html("`unclosed") == "`unclosed"
    assert tg._md_to_tg_html("3 * 4 * 5") == "3 * 4 * 5"        # math ≠ italic
    # mid-stream fragment (progressive edits) never yields a broken tag
    assert tg._md_to_tg_html("here is **bol") == "here is **bol"


def test_converter_links_and_bullets():
    assert (tg._md_to_tg_html("[docs](https://x.com)")
            == '<a href="https://x.com">docs</a>')
    assert tg._md_to_tg_html("- one\n- two") == "· one\n· two"
    assert tg._md_to_tg_html("* item") == "· item"


def test_converter_quote_blocks_become_blockquotes():
    assert (tg._md_to_tg_html("here:\n> smol post idea\n\nthanks")
            == "here:\n<blockquote>smol post idea</blockquote>\n\nthanks")
    # contiguous quote lines collapse into ONE quote block
    assert (tg._md_to_tg_html("> line one\n> line two")
            == "<blockquote>line one\nline two</blockquote>")


# ---------------- clip + 4096 guard ----------------

def test_format_clips_before_tagging():
    out = tg._format_tg("x" * 6000)
    assert len(out) <= tg.MSG_LIMIT and out.endswith("…")


def test_format_falls_back_to_plain_when_tags_would_overflow():
    pathological = "**a** " * 1500            # tag overhead pushes past 4096
    out = tg._format_tg(pathological)
    assert len(out) <= 4096
    assert "<b>" not in out                   # plain, never a broken tag


# ---------------- parse_mode on the wire ----------------

def test_send_message_carries_html_parse_mode(fake_api):
    r = tg.send_message(1, "**bold** & <raw>")
    assert r["ok"]
    params = fake_api.of("sendMessage")[-1]
    assert params["parse_mode"] == "HTML"
    assert params["text"] == "<b>bold</b> &amp; &lt;raw&gt;"


def test_stream_edits_carry_html_parse_mode(fake_api, monkeypatch):
    monkeypatch.setattr(tg, "STREAM_EDIT_MIN_S", 0.0)
    monkeypatch.setattr(tg, "STREAM_EDIT_EVERY", 1)
    parts = ["trained ", "a model ", "today.\n", "it ", "hallucinated ", "me."]
    assert tg.send_stream(1, _chunks(*parts))["ok"]
    edits = fake_api.of("editMessageText")
    assert edits and all(p.get("parse_mode") == "HTML" for p in edits)
    assert edits[-1]["text"] == tg._md_to_tg_html("".join(parts))


def test_send_retries_plain_once_on_entity_rejection(monkeypatch):
    api = ParseRejectAPI()
    monkeypatch.setattr(tg, "_api", api)
    monkeypatch.setattr(tg, "bot_token", lambda: "123:FAKE")
    r = tg.send_message(1, "**bold** that telegram refuses to parse")
    assert r["ok"]                                   # delivery never fails
    sends = api.of("sendMessage")
    assert len(sends) == 2                           # exactly one retry
    assert sends[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in sends[1]
    assert "<b>" not in sends[1]["text"]             # retry went out plain


def test_edit_retries_plain_once_on_entity_rejection(monkeypatch):
    api = ParseRejectAPI()
    monkeypatch.setattr(tg, "_api", api)
    monkeypatch.setattr(tg, "bot_token", lambda: "123:FAKE")
    r = tg._api_edit_text("123:FAKE", 1, 55, "**bold** edit")
    assert r.status_code == 200
    edits = api.of("editMessageText")
    assert len(edits) == 2
    assert edits[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in edits[1]


# ---------------- Problem 1: the TG assistant persona ----------------

def test_tg_chat_uses_the_tg_assistant_persona(monkeypatch):
    """The chat path must NOT use the dashboard write-assistant prompt —
    the X-post voice leaks into conversations from there."""
    import openstanley.gen.chat as chat_mod

    captured: dict[str, str] = {}

    def fake_stream(llm_cfg, system, user):
        captured["system"] = system
        return iter(["Sure — checked the calendar."])

    monkeypatch.setattr(chat_mod, "llm_chat_stream", fake_stream)
    monkeypatch.setattr(chat_mod, "llm_chat", lambda *a, **k: "")
    cfg = _cfg()
    tg._sessions.clear()
    "".join(tg.chat_reply_tg_stream(cfg, 5, "hello there"))
    sys_prompt = captured["system"]
    assert "Telegram" in sys_prompt                       # the TG persona block
    assert "clean, warm, direct" in sys_prompt            # assistant voice rule
    assert "only inside post drafts" in sys_prompt        # X voice is scoped
    assert sys_prompt != chat_mod._system(cfg, "hello there")  # not the X voice
