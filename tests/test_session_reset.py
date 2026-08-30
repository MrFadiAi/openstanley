"""05:00 daily session reset — both frontends start fresh each morning
(like Hermes; owner request 2026-08-30)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"
os.environ.setdefault("OPENSTANLEY_NO_TELEGRAM", "1")
os.environ.setdefault("OPENSTANLEY_NO_SMOKE", "1")

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.gen import chat as chat_mod                      # noqa: E402


def test_reset_bounds_history():
    """After reset, _history_turn sees only post-reset messages."""
    db.set_setting("chat_session_reset_at", "")
    import time as _time
    db.add_chat_message("user", "before the reset marker probe zebra")
    _time.sleep(1.1)  # ts precision is seconds — cross a boundary
    from openstanley.integrations import telegram as tg
    tg.reset_sessions()
    _time.sleep(1.1)  # the after-message must land past the watermark
    db.add_chat_message("user", "after the reset marker probe yjeta")
    hist = db.chat_history(10)
    texts = [h["content"] for h in hist]
    assert any("yjeta" in t for t in texts), "post-reset visible"
    assert not any("zebra" in t for t in texts), "pre-reset hidden"
    db.set_setting("chat_session_reset_at", "")


def test_tg_window_cleared():
    from openstanley.integrations import telegram as tg
    tg._sessions[999] = [{"role": "user", "content": "old"}]
    n = tg.reset_sessions()
    assert 999 not in tg._sessions, "in-memory window cleared"
    db.set_setting("chat_session_reset_at", "")
