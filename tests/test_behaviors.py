"""Autonomous behaviors — owner-toggled capabilities for the create loop."""
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

from openstanley.gen import behaviors as beh                      # noqa: E402


def _reset():
    db.set_setting(beh.BEHAVIOR_SETTING, {})


def test_prebuilt_defaults_off():
    _reset()
    for b in beh.get_behaviors():
        assert b["enabled"] is False
    assert beh.enabled("link_first_reply") is False


def test_toggle_persists_and_validates():
    _reset()
    assert beh.set_behavior("link_first_reply", True) is True
    assert beh.enabled("link_first_reply") is True
    assert beh.set_behavior("nope", True) is False  # unknown id refused
    _reset()


def test_shape_draft_link_first_reply():
    _reset()
    beh.set_behavior("link_first_reply", True)
    idea = {"source_x_id": "111", "source_handle": "maker"}
    link = "https://x.com/maker/status/111"
    draft = {"text": f"great tool alert {link} more words",
             "kind": "post", "meta": {}}
    out = beh.shape_draft(draft, idea)
    assert link not in out["text"], "link leaves the body"
    assert out["meta"]["link_reply"] == link
    _reset()


def test_shape_draft_quote_landmarks():
    _reset()
    beh.set_behavior("quote_landmarks", True)
    idea = {"source_x_id": "222", "source_handle": "bigco",
            "score": 8.5}
    draft = {"text": "landmark launch take", "kind": "post", "meta": {}}
    out = beh.shape_draft(draft, idea)
    assert out["kind"] == "quote" and out["quote_of"] == "222"
    # below the score bar stays a plain post
    low = beh.shape_draft({"text": "x", "kind": "post", "meta": {}},
                          {"source_x_id": "222", "source_handle": "h",
                           "score": 6.0})
    assert low["kind"] == "post"
    _reset()


def test_off_means_untouched():
    _reset()
    idea = {"source_x_id": "333", "source_handle": "h", "score": 9.0}
    draft = {"text": "plain stays plain", "kind": "post", "meta": {}}
    out = beh.shape_draft(draft, idea)
    assert out["kind"] == "post" and "link_reply" not in out.get("meta", {})
