"""User rule 2026-08-20: no em/en dashes in any generated text — they read as AI."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENSTANLEY_NO_SCHEDULER", "1")

from openstanley.core.text import scrub_ai_punctuation  # noqa: E402


def test_scrub_replaces_all_dash_kinds():
    assert scrub_ai_punctuation("hot take \u2014 agents ship") == "hot take, agents ship"
    assert "\u2013" not in scrub_ai_punctuation("a\u2013b \u2013 c")
    assert " -- " not in scrub_ai_punctuation("x -- y")
    assert "\u2212" not in scrub_ai_punctuation("minus \u2212 sign")


def test_scrub_cleans_double_commas_and_keeps_clean_text():
    assert scrub_ai_punctuation("one \u2014 \u2014 two") == "one, two"
    assert scrub_ai_punctuation("plain text, stays") == "plain text, stays"
    assert scrub_ai_punctuation("") == ""


def test_db_add_draft_scrubs_at_save_boundary():
    from openstanley.core import db
    db.init_db()
    did = db.add_draft(text="lesson \u2014 ship it \u2013 then talk", acct=1)
    row = db.get_draft(did)
    assert "\u2014" not in row["text"] and "\u2013" not in row["text"]
    db.update_draft(did, acct=1, text="edited \u2014 again")
    assert "\u2014" not in db.get_draft(did)["text"]


def test_thread_tweets_scrubbed_too():
    from openstanley.core import db
    db.init_db()
    t = ["hook \u2014 one", "body \u2013 two", "tail"]
    did = db.add_draft(text=t[0], thread=t, acct=1)
    row = db.get_draft(did)
    assert all("\u2014" not in x and "\u2013" not in x for x in row["thread"])
