"""LLM robustness — the two failure modes the QA loop caught in production
on 2026-08-20 (agent_log level=error):

1. drafts' experimental temperature (1.15) → z.ai HTTP 400 "temperature
   parameter is illegal, range [0,1]" → whole create draft lost.
2. engage replies: GLM emits JSON with literal newlines inside strings →
   json.loads fails → reply draft lost.

Pinned here so neither can regress silently.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENSTANLEY_NO_SCHEDULER", "1")

from openstanley.gen.llm import extract_json, _repair_json_strings  # noqa: E402


def test_extract_json_repairs_literal_newlines_in_strings():
    """The exact 06:30 engage failure: raw newlines inside {"reply": "..."}."""
    text = ('{"reply": "i don\'t sleep at all' + chr(10) + chr(10) +
            'maybe the wanting comes after the thinking"}')
    out = extract_json(text)
    assert "wanting comes after" in out["reply"]
    assert "don't sleep" in out["reply"]


def test_extract_json_repairs_tabs_and_in_substring_mode():
    text = "prose {\"k\": \"a" + chr(9) + "b" + chr(10) + "c\"} tail"
    out = extract_json(text)
    # parsed value keeps the REAL control chars — they were just escaped in transit
    assert out == {"k": "a" + chr(9) + "b" + chr(10) + "c"}


def test_extract_json_still_rejects_true_garbage():
    import pytest
    from openstanley.gen.llm import LLMError
    with pytest.raises(LLMError):
        extract_json("no json here at all")


def test_plain_and_fenced_json_untouched():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json("```json\n{\"b\": 2}\n```") == {"b": 2}


def test_repair_only_touches_quoted_regions():
    raw = 'x {"a": "n"} y'
    assert _repair_json_strings(raw) == raw  # nothing to fix → byte-identical


def test_temperature_clamped_to_provider_range():
    """chat() must never send temperature > 1 — z.ai 400s the whole call."""
    import json as _json
    import os as _os
    import openstanley.gen.llm as llm

    _os.environ["OPENSTANLEY_TEST_LLM_KEY"] = "test-key"
    cfg = llm.LLMConfig(base_url="https://x", model="m", api_key_env="OPENSTANLEY_TEST_LLM_KEY",
                        transport="openai")

    sent = {}

    def fake_post(url, json=None, timeout=None, **kw):  # noqa: A002
        sent.update(json or {})
        return type("R", (), {
            "status_code": 200,
            "json": lambda self: {"choices": [{"message": {"content": "{\"ok\": true}"}}]},
        })()

    real_post = llm.httpx.post
    llm.httpx.post = fake_post
    try:
        out = llm.chat(cfg, "s", "u", temperature=1.15)
        assert out  # call succeeded
        assert sent["temperature"] == 1.0, sent["temperature"]  # clamped at cap
    finally:
        llm.httpx.post = real_post


def test_draft_temperature_ladder_within_provider_range():
    """The ladder itself must top out at 1.0, not rely on the clamp."""
    import openstanley.gen.drafts as drafts
    import inspect
    src = inspect.getsource(drafts)
    assert "1.15" not in src
    assert '"experimental": 1.0' in src


import os as _os2
_os2.environ["OPENSTANLEY_TEST_LLM_KEY"] = "test-key"

# ---------- empty replies must DIAGNOSE, never pass silently ----------
# Live incident 2026-08-27: GLM always emits a thinking block first; a small
# max_tokens cap lets thinking eat the whole budget → zero text blocks. The
# old parser returned "" silently (smoke showed a healthy LLM as red with a
# useless "empty reply"; drafts got blank strings).

def _resp(payload):
    return type("R", (), {"status_code": 200, "json": lambda self: payload})()


def test_anthropic_thinking_ate_budget_raises_with_shape():
    import openstanley.gen.llm as llm

    cfg = llm.LLMConfig(base_url="https://x", model="m",
                        api_key_env="OPENSTANLEY_TEST_LLM_KEY",
                        transport="anthropic")

    def fake_post(url, json=None, timeout=None, **kw):  # noqa: A002
        return _resp({"stop_reason": "max_tokens",
                      "content": [{"type": "thinking", "thinking": "…"}]})

    real_post = llm.httpx.post
    llm.httpx.post = fake_post
    try:
        out = llm.chat(cfg, "s", "u", retries=0)
        raise AssertionError(f"should have raised, got {out!r}")
    except llm.LLMError as e:
        assert "stop_reason=max_tokens" in str(e), str(e)
        assert "thinking" in str(e), "the block shape must name the culprit"
    finally:
        llm.httpx.post = real_post


def test_anthropic_normal_reply_still_parses():
    import openstanley.gen.llm as llm

    cfg = llm.LLMConfig(base_url="https://x", model="m",
                        api_key_env="OPENSTANLEY_TEST_LLM_KEY",
                        transport="anthropic")

    def fake_post(url, json=None, timeout=None, **kw):  # noqa: A002
        return _resp({"stop_reason": "end_turn",
                      "content": [{"type": "thinking", "thinking": "x"},
                                  {"type": "text", "text": "pong"}]})

    real_post = llm.httpx.post
    llm.httpx.post = fake_post
    try:
        assert llm.chat(cfg, "s", "u", retries=0) == "pong"
    finally:
        llm.httpx.post = real_post


def test_openai_empty_content_raises_with_finish_reason():
    import openstanley.gen.llm as llm

    cfg = llm.LLMConfig(base_url="https://x", model="m",
                        api_key_env="OPENSTANLEY_TEST_LLM_KEY",
                        transport="openai")

    def fake_post(url, json=None, timeout=None, **kw):  # noqa: A002
        return _resp({"choices": [{"message": {"content": ""},
                                   "finish_reason": "length"}]})

    real_post = llm.httpx.post
    llm.httpx.post = fake_post
    try:
        llm.chat(cfg, "s", "u", retries=0)
        raise AssertionError("should have raised")
    except llm.LLMError as e:
        assert "finish_reason=length" in str(e), str(e)
    finally:
        llm.httpx.post = real_post


def test_smoke_llm_probe_budget_fits_thinking_block():
    """The probe's max_tokens cap must leave room for GLM's mandatory
    thinking block + the one-word answer — 16 proved too small live."""
    import dataclasses as _dc
    from openstanley.core.config import Config
    from openstanley.system import smoke

    seen = {}

    def fake_chat(cfg, system="", user="", **kw):
        seen["max_tokens"] = cfg.max_tokens
        return "pong"

    real = smoke.llm_chat
    smoke.llm_chat = fake_chat
    try:
        assert smoke._default_llm(Config().llm) == "pong"
        assert seen["max_tokens"] >= 64, seen
    finally:
        smoke.llm_chat = real


def test_stream_empty_reply_raises_not_blank(monkeypatch):
    """Live 2026-08-28 (the agent's own apology: 'replies glitched out
    empty'): a stream that closes with ZERO text deltas (all thinking)
    yielded nothing and the chat stored a blank reply silently. Both
    stream transports now raise like their non-streaming siblings."""
    import openstanley.gen.llm as llm
    import os as _os
    _os.environ["OPENSTANLEY_TEST_LLM_KEY"] = "test-key"
    cfg = llm.LLMConfig(base_url="https://x", model="m",
                        api_key_env="OPENSTANLEY_TEST_LLM_KEY",
                        transport="anthropic")

    class FakeStream:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        status_code = 200
        def iter_lines(self):
            # all-thinking: block deltas with thinking only, then end —
            # exactly the live failure shape
            return [
                b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"..."}}',
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
            ]

    monkeypatch.setattr(llm.httpx, "stream",
                        lambda *a, **k: FakeStream())
    got = []
    try:
        for tok in llm.chat_stream(cfg, "s", "u"):
            got.append(tok)
        raise AssertionError(f"should raise, yielded {got!r}")
    except llm.LLMError as e:
        assert "empty stream" in str(e) and "end_turn" in str(e), str(e)


def test_stream_with_text_still_yields(monkeypatch):
    import openstanley.gen.llm as llm
    import os as _os
    _os.environ["OPENSTANLEY_TEST_LLM_KEY"] = "test-key"
    cfg = llm.LLMConfig(base_url="https://x", model="m",
                        api_key_env="OPENSTANLEY_TEST_LLM_KEY",
                        transport="anthropic")

    class FakeStream:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        status_code = 200
        def iter_lines(self):
            return [
                b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"hmm"}}',
                b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}',
            ]

    monkeypatch.setattr(llm.httpx, "stream",
                        lambda *a, **k: FakeStream())
    assert "".join(llm.chat_stream(cfg, "s", "u")) == "hello"
