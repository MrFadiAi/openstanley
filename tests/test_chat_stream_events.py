"""v0.3.1 chat SSE event shapes: thinking_steps, tool, approval + voice tuning.

The stream must open with a thinking_steps trace (steps + context chunks),
emit structured tool events (no legacy "action"), and surface every post
candidate as an approval event carrying the algorithm score + voice match.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import chat                                   # noqa: E402

FENCE = "`" * 3


def _fake_stream(cfg, system, user, temperature=None):
    yield "here is your post:\n> an ugly first version teaches what slides never will\n\n"
    yield (f'{FENCE}action\n{{"tool": "pick_idea", "args": {{}}}}\n{FENCE}')
    yield "\nqueued."


def test_stream_event_shapes():
    db.add_idea("ship ugly", "build in public lesson", "one-liner", "test", 8.5)
    orig = chat.llm_chat_stream
    chat.llm_chat_stream = _fake_stream
    try:
        events = list(chat.chat_reply_stream(Config(), "write me a post and pick an idea"))
    finally:
        chat.llm_chat_stream = orig

    types = [e["type"] for e in events]
    assert types[0] == "thinking_steps", "stream must open with the context trace"
    assert "action" not in types, "legacy action events must be replaced by tool"
    assert "token" in types and "tool" in types and "approval" in types
    assert types[-1] == "done"

    ts = events[0]
    assert ts["steps"] and all("primary" in s for s in ts["steps"])
    assert isinstance(ts["chunks"], list)

    tool = next(e for e in events if e["type"] == "tool")
    assert tool["name"] == "pick_idea" and tool["ok"] is True
    assert "top" in tool["result"] and "algorithm_preview" in tool["result"]

    approval = next(e for e in events if e["type"] == "approval")
    cand = approval["candidate"]
    assert cand["text"].startswith("an ugly first version")
    assert 0 <= cand["alg"]["score"] <= 100 and cand["alg"]["factors"]
    assert "voice_match" in cand and "language" in cand

    done = events[-1]
    assert done["candidates"] and done["candidates"][0]["text"] == cand["text"]
    assert done["reply"] and "action" not in done["reply"], "done carries the cleaned reply"
    print("[ok] stream: thinking_steps → tokens → tool → approval → done")


def test_voice_tune_maps_to_temperature_and_prompt():
    db.set_setting("voice_temperature", "experimental")
    db.set_setting("voice_formality", 90)
    assert chat._llm_temperature() == 0.95
    prompt = chat._tune_prompt(chat._voice_tune())
    assert "polished" in prompt and "VOICE TUNING" in prompt
    # restore defaults so other tests are unaffected
    for k, v in (("voice_temperature", "bold"), ("voice_formality", 50)):
        db.set_setting(k, v)
    assert chat._llm_temperature() == 0.7
    print("[ok] voice tune: experimental→0.95, formality→prompt line")
