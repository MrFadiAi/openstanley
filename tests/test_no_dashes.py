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
    try:
        row = db.get_draft(did)
        assert all("\u2014" not in x and "\u2013" not in x for x in row["thread"])
    finally:  # never leak rows into other suites' deterministic ranges
        with db.connect() as c:
            c.execute("DELETE FROM drafts WHERE id=?", (did,))


def test_post_band_floor_is_60pct_of_avg(tmp_path, monkeypatch):
    """Short-draft guard: the lock's floor must sit at 60% of the account's
    average, not 40% — 69-char 'posts' must fail for a 167-char account."""
    from openstanley.gen import voice_lock
    monkeypatch.setattr(voice_lock, "voice_md_path",
                        lambda acct=None: tmp_path / "voice.md")
    stats = {"avg_length_chars": 167.0}
    path = voice_lock.write_voice_md(stats, acct=1)
    band = [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.startswith("length_band_post")][0]
    lo = int(band.split(":")[1].split("-")[0])
    assert lo >= 100, band   # 60% of 167


def test_err_str_never_blanks_the_cause():
    """twikit/asyncio exceptions str() to '' — err_str keeps the type visible
    (live logs showed 'search failed for X: ' with the cause erased)."""
    from openstanley.core.text import err_str

    class Silent(Exception):
        pass

    assert err_str(ValueError("boom")) == "boom"
    out = err_str(Silent())
    assert out and "Silile" not in out and "Silent" in out


def test_total_dash_ban_owner_directive():
    """Owner directive 2026-08-29: NEVER any dash — list-bullet hyphens and
    hyphenated compounds survived the em/en scrub. URLs are exempt."""
    from openstanley.core.text import scrub_ai_punctuation as sc
    assert "-" not in sc("AI-driven content with - bullets and — em")
    assert sc("- item one\n- item two") == "item one\nitem two"
    url = "https://example.com/x-y"
    assert url in sc(f"check {url} now"), "URL dashes are structural"
    assert "www.foo-bar.com/baz" in sc("see www.foo-bar.com/baz")
