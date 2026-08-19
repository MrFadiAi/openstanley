"""v0.4.0 voice lock — hermetic (dryrun X, test DB, no network).

Covers the deterministic scorer (every violation class, clean pass, length
bands per kind, misspelling band in both directions), voice.md parsing
(present / absent → neutral + warn), the borderline LLM fix path (fixed wins
when better, loses when worse), threshold from config vs db override, the
disabled short-circuit, and the pipeline wiring: rejected drafts are never
stored + are logged, and create / engage / mentions / chat drafts carry
meta.voice.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openstanley.core import db                                    # noqa: E402
db.init_db()

from openstanley.core.config import Config                         # noqa: E402
from openstanley.gen import chat as chat_mod                       # noqa: E402
from openstanley.gen import drafts as drafts_mod                   # noqa: E402
from openstanley.gen import mentions as mentions_mod               # noqa: E402
from openstanley.gen import replies as replies_mod                 # noqa: E402
from openstanley.gen import voice_lock                             # noqa: E402

# the quirky AI-agent persona the brief describes: lowercase, no emoji,
# no hashtags, some deliberate misspellings, short choppy posts
PERSONA_MD = """# Voice
lowercase_first: true
length_band_post: 40-210
length_band_reply: 15-150
emoji_max: 0
hashtags_max: 0
misspelling_band: 2.0-20.0
"""

CLEAN_POST = ("i speek abt the craft of shipping small things daily "
              "and the wins add up slow")


@pytest.fixture()
def persona(tmp_path, monkeypatch):
    """Point the lock at a fresh persona voice.md; returns a rewriter."""
    def _write(content: str = PERSONA_MD) -> None:
        p = tmp_path / "voice.md"
        p.write_text(content, encoding="utf-8")
        monkeypatch.setattr(voice_lock, "voice_md_path",
                            lambda acct=None: p)
    _write()
    return _write


def _clean_db():
    for t in ("posts", "drafts", "engagements", "seen_mentions", "ideas",
              "agent_log", "chat_messages"):
        with db.connect() as c:
            c.execute(f"DELETE FROM {t}")
    db.set_setting("style_profile", None)
    db.set_setting("voice_lock_enabled", None)     # back to config default
    db.set_setting("voice_lock_threshold", None)


def _no_llm(monkeypatch):
    """The LLM must never be reached in deterministic tests."""
    def boom(*a, **kw):  # noqa: ANN001, ANN002
        raise AssertionError("LLM called outside the borderline fix path")
    monkeypatch.setattr(voice_lock, "chat", boom)


# ---------------- deterministic scorer ----------------

def test_clean_persona_text_passes_without_llm(persona, monkeypatch):
    _no_llm(monkeypatch)
    cfg = Config()
    vc = voice_lock.check_draft(cfg, CLEAN_POST, "post")
    assert vc.score_0_100 >= 90 and vc.passed and not vc.violations
    assert vc.fixed_text is None
    assert vc.meta() == {"score": vc.score_0_100, "checked": True}


def test_each_violation_class_fires(persona):
    rules = voice_lock.load_persona_rules()
    cases = [
        ("this is a game-changer for the whole team and it ships today",
         "corporate phrase"),
        ("Started with an uppercase letter like a corporate account would",
         "starts uppercase"),
        ("added a tiny thing 🚀 to the build today for the whole team",
         "emoji"),
        ("we shipped the thing and it actually works!! for everyone here",
         "exclamation stacking"),
        ("thoughts on shipping #build #learn #ai every single day now",
         "hashtag wall"),
        ("ok", "length 2 outside persona band"),
    ]
    for text, marker in cases:
        score, violations = voice_lock.score_deterministic(text, "post", rules)
        assert any(marker in v for v in violations), (marker, violations)
        assert score < 100


def test_length_bands_per_kind(persona):
    rules = voice_lock.load_persona_rules()
    text = "a" * 100  # fine for a post (40-210), too long for a reply (15-150)?
    assert not any("length" in v for v in
                   voice_lock.score_deterministic(text, "post", rules)[1])
    # tighten the reply band: same text must violate as a reply
    tight = dict(rules)
    tight["bands"] = {**rules["bands"], "reply": (15, 60)}
    _, violations = voice_lock.score_deterministic(text, "reply", tight)
    assert any("length 100 outside persona band 15-60" in v for v in violations)


def test_misspelling_band_both_directions(persona):
    rules = voice_lock.load_persona_rules()
    polished = ("we should carefully consider the strategic implications "
                "of this particular approach before proceeding further")
    assert any("too polished" in v for v in
               voice_lock.score_deterministic(polished, "post", rules)[1])
    overloaded = "i speek teh smol wat and wen i spek teh wat i speek it thru"
    assert any("misspelling overload" in v for v in
               voice_lock.score_deterministic(overloaded, "post", rules)[1])


# ---------------- voice.md parsing ----------------

def test_voice_md_parsing_present(persona):
    rules = voice_lock.load_persona_rules()
    assert rules["source"] == "brain"
    assert rules["lowercase_first"] is True
    assert rules["bands"]["post"] == (40, 210)
    assert rules["bands"]["reply"] == (15, 150)
    assert rules["emoji_max"] == 0
    assert rules["hashtags_max"] == 0
    assert rules["misspelling_band"] == (2.0, 20.0)


def test_voice_md_absent_falls_back_to_neutral_and_warns_once(
        tmp_path, monkeypatch):
    _clean_db()
    monkeypatch.setattr(voice_lock, "_warned_missing", False)
    monkeypatch.setattr(voice_lock, "voice_md_path",
                        lambda acct=None: tmp_path / "missing.md")
    rules = voice_lock.load_persona_rules()
    assert rules["source"] == "neutral" and rules["lowercase_first"] is False
    # neutral rules do not punish casing (unknown → unchecked)
    _, violations = voice_lock.score_deterministic(
        "Started uppercase but nobody knows the persona yet", "post", rules)
    assert not any("uppercase" in v for v in violations)
    voice_lock.load_persona_rules()  # second load → still exactly one warn
    with db.connect() as c:
        rows = c.execute("SELECT COUNT(*) AS n FROM agent_log "
                         "WHERE loop='voice' AND message LIKE "
                         "'voice.md missing%'").fetchone()
    assert rows["n"] == 1


def test_write_voice_md_derives_scan_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_lock, "voice_md_path",
                        lambda acct=None: tmp_path / "voice.md")
    stats = {
        "avg_length_chars": 100,
        "emoji": {"per_post": 0.0, "top": []},
        "hashtags": {"pct_with": 0.0, "per_post": 0.0},
        "casing": {"pct_lowercase_start": 0.9},
        "misspellings_per_100_words": 4.0,
    }
    voice_lock.write_voice_md(stats)
    rules = voice_lock.load_persona_rules()
    assert rules["source"] == "brain"
    assert rules["lowercase_first"] is True
    assert rules["bands"]["post"] == (40, 180)
    assert rules["emoji_max"] == 0 and rules["hashtags_max"] == 0
    assert rules["misspelling_band"] == (2.0, 8.5)


# ---------------- borderline LLM fix path ----------------

BORDERLINE = "Check the new thing 🚀🔥 it speek to the whole team quite well"


def test_borderline_llm_fix_wins_when_better(persona, monkeypatch):
    assert voice_lock.score_deterministic(BORDERLINE, "post")[0] == 58
    fixed = "check the new thing it speek to the whole team quite well"
    calls = []

    def fake_chat(cfg, system, user, **kw):
        calls.append(user)
        return '{"text": "' + fixed + '"}'

    monkeypatch.setattr(voice_lock, "chat", fake_chat)
    vc = voice_lock.check_draft(Config(), BORDERLINE, "post")
    assert len(calls) == 1, "borderline → exactly one focused LLM call"
    assert vc.fixed_text == fixed
    assert vc.score_0_100 == 100 and vc.passed
    assert vc.meta()["fixed"] is True


def test_borderline_fix_loses_when_worse(persona, monkeypatch):
    def worse(cfg, system, user, **kw):
        return ('{"text": "Excited to share this game-changer!! '
                '🚀🚀🚀 with the whole team today"}')

    monkeypatch.setattr(voice_lock, "chat", worse)
    vc = voice_lock.check_draft(Config(), BORDERLINE, "post")
    assert vc.fixed_text is None, "a worse rewrite is discarded"
    assert vc.score_0_100 == 58 and not vc.passed
    assert "fixed" not in vc.meta()


# ---------------- threshold + enabled ----------------

def test_threshold_from_config_and_db_override(persona, monkeypatch):
    _no_llm(monkeypatch)
    _clean_db()
    cfg = Config()
    text = "Started with uppercase but otherwise fine and long enough here ok"
    assert voice_lock.score_deterministic(text, "post")[0] == 88
    cfg.agent.voice_lock_threshold = 90
    assert not voice_lock.check_draft(cfg, text, "post").passed
    cfg.agent.voice_lock_threshold = 75  # default: 88 passes
    assert voice_lock.check_draft(cfg, text, "post").passed
    db.set_setting("voice_lock_threshold", 95)  # UI slider wins over config
    assert not voice_lock.check_draft(cfg, text, "post").passed
    db.set_setting("voice_lock_threshold", None)


def test_disabled_lock_passes_text_through(persona, monkeypatch):
    _no_llm(monkeypatch)
    _clean_db()
    cfg = Config()
    db.set_setting("voice_lock_enabled", False)
    text, meta = voice_lock.apply_voice_lock(
        cfg, "Excited to share our game-changer!!", "post")
    assert text == "Excited to share our game-changer!!" and meta is None
    db.set_setting("voice_lock_enabled", None)


# ---------------- pipeline wiring ----------------

def test_create_rejected_draft_not_stored_and_logged(persona, monkeypatch):
    _clean_db()
    _no_llm(monkeypatch)  # deep-fail text must not even try the LLM fix
    cfg = Config()
    db.add_idea("off-voice test", "angle", "one-liner", "test", 9.0)

    def corporate_llm(cfg, system, user, **kw):
        return ('{"tweet": "Excited to share our game-changer workflow!! '
                '🚀"}')

    monkeypatch.setattr(drafts_mod, "chat", corporate_llm)
    ids = drafts_mod.generate_drafts(cfg, 1)
    assert ids == []
    with db.connect() as c:
        (n,) = c.execute("SELECT COUNT(*) FROM drafts").fetchone()
        rejected = c.execute("SELECT message FROM agent_log WHERE loop='voice' "
                             "AND message LIKE 'voice lock rejected%'").fetchall()
    assert n == 0
    assert rejected and "score " in rejected[0]["message"]
    # the idea stays fresh — a retry can pick it up again
    assert len(db.fresh_ideas(limit=5)) == 1


def test_create_draft_carries_meta_voice(persona, monkeypatch):
    _clean_db()
    cfg = Config()
    db.add_idea("clean test", "angle", "one-liner", "test", 9.0)

    def clean_llm(cfg, system, user, **kw):
        return '{"tweet": "' + CLEAN_POST + '"}'

    monkeypatch.setattr(drafts_mod, "chat", clean_llm)
    ids = drafts_mod.generate_drafts(cfg, 1)
    assert len(ids) == 1
    d = db.get_draft(ids[0])
    assert d["meta"]["voice"]["checked"] is True
    assert d["meta"]["voice"]["score"] >= 75
    assert d["text"] == CLEAN_POST


def test_engage_niche_reply_carries_meta_voice(persona, monkeypatch):
    _clean_db()
    db.set_setting("style_profile", {"stats": {"topics": ["building"]}})
    cfg = Config()
    from datetime import datetime, timedelta
    db.upsert_post({"x_id": "live1", "author_handle": "poster", "is_own": 0,
                    "created_at": (datetime.now() -
                                   timedelta(hours=1)).isoformat(
                                       timespec="seconds"),
                    "text": "thoughts on building tools and shipping things?",
                    "likes": 350, "reposts": 80, "replies": 10,
                    "impressions": 12000})

    def clean_llm(cfg, system, user, **kw):
        return '{"reply": "sharp angle — answered with a number, no fluff"}'

    monkeypatch.setattr(replies_mod, "chat", clean_llm)
    ids = replies_mod.draft_niche_replies(cfg, limit=3)
    assert len(ids) == 1
    d = db.get_draft(ids[0])
    assert d["meta"]["voice"]["checked"] is True
    assert d["meta"]["voice"]["score"] >= 75


def test_mention_reply_voice_meta_and_rejection(persona, monkeypatch):
    _clean_db()
    cfg = Config()
    with db.connect() as c:
        c.execute("INSERT INTO seen_mentions (x_id, author, text, created_at, "
                  "first_seen, handled) VALUES (?,?,?,?,?,0)",
                  ("m1", "someone", "@openstanley thoughts?",
                   "2026-08-19T09:00:00", "2026-08-19T09:01:00"))
    mention = mentions_mod.pending_mentions()[0]

    def clean_llm(cfg, system, user, **kw):
        return '{"reply": "sharp angle — answered with a number, no fluff"}'

    def corporate_llm(cfg, system, user, **kw):
        return '{"reply": "Excited to share this with you!! 🚀"}'

    monkeypatch.setattr(mentions_mod, "chat", clean_llm)
    did = mentions_mod.draft_mention_reply(cfg, mention)
    assert did and db.get_draft(did)["meta"]["voice"]["checked"] is True

    with db.connect() as c:  # a second pending mention gets off-voice junk
        c.execute("INSERT INTO seen_mentions (x_id, author, text, created_at, "
                  "first_seen, handled) VALUES (?,?,?,?,?,0)",
                  ("m2", "else", "@openstanley again?",
                   "2026-08-19T09:05:00", "2026-08-19T09:06:00"))
    monkeypatch.setattr(mentions_mod, "chat", corporate_llm)
    assert mentions_mod.draft_mention_reply(cfg,
                                            mentions_mod.pending_mentions()[0]) \
        is None
    row = mentions_mod.recent_mentions(limit=5)[0]  # newest first → m2
    assert row["x_id"] == "m2" and row["handled"] == 0, \
        "off-voice mention stays pending for a retry"


def test_chat_candidates_and_saved_draft_carry_voice(persona, monkeypatch):
    _clean_db()
    cfg = Config()
    reply = ("here you go:\n"
             f"> {CLEAN_POST}\n\n"
             "want it scheduled?")

    def fake_llm(llm_cfg, system=None, user="", **kw):
        return reply

    monkeypatch.setattr(chat_mod, "llm_chat", fake_llm)
    result = chat_mod.chat_reply(cfg, "write a post about shipping")
    assert len(result["candidates"]) == 1
    cand = result["candidates"][0]
    assert cand["voice"]["checked"] is True and cand["voice"]["score"] >= 75

    did = chat_mod.draft_from_chat(cfg, cand["text"])  # human-approved → stored
    d = db.get_draft(did)
    assert d["meta"]["voice"]["score"] >= 75
    assert d["text"] == cand["text"], "the human-approved text is never swapped"
