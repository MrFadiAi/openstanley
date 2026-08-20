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
