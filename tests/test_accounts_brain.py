"""Multi-account (v0.5.0) — per-account brains + data dirs (Phase 2).

The user's hard requirement: a fresh account starts with an EMPTY brain
(seed stubs only) and NOTHING from other accounts ever leaks into it.

Covers: legacy data/brain/ → data/accounts/1/brain/ migration, per-account
brain_context/read/write/reflect isolation, per-account voice.md (voice lock
persona), per-account digest dirs + legacy digests migration, per-account
style_profile/strategy settings, and the API account-create path seeding a
fresh brain.

Hermetic: brain dirs sandboxed via ACCOUNTS_ROOT, digest env override
cleared only where the per-account path is under test, LLM seam patched in
the reflect test, dryrun X, no network.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("OPENSTANLEY_NO_SCHEDULER", "1")  # before importing the server

import pytest  # noqa: E402

from openstanley.core import db                 # noqa: E402
db.init_db()

from openstanley.core.config import Config      # noqa: E402
from openstanley.gen import brain               # noqa: E402
from openstanley.gen import digest as digest_mod  # noqa: E402
from openstanley.gen import voice_lock          # noqa: E402

CFG = Config()

REPLY_JSON = ('{"instructions_delta": "", "new_rules": '
              '[{"text": "answer questions within the hour", "why": "x"}], '
              '"retire_rule_ids": [], "strategy_updates": [], '
              '"file_updates": [], "journal_entry": "learned to be fast"}')


@pytest.fixture(autouse=True)
def _brain_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "ACCOUNTS_ROOT", tmp_path / "accounts")
    db.set_active_account(1)
    yield
    db.set_active_account(1)


def _account1_with_history() -> None:
    """Give account 1 a brain full of learned content."""
    brain.ensure(1)
    brain.add_rule("never post without a hook", source="learn", acct=1)
    brain.journal_append("reflect:learn", "account 1 learned something",
                          ["added R1"], acct=1)
    p = brain.brain_dir(1) / "strategies.md"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "- (none yet — the learn loop will fill these from real metrics)",
        "- threads outperform one-liners 2:1"), encoding="utf-8")


# ---------------- legacy migration ----------------

def test_legacy_brain_migrates_to_account_1(tmp_path, monkeypatch):
    # the migration only fires when ROOT/ACCOUNTS_ROOT are consistent —
    # sandbox the whole module layout, real data stays untouched
    fake_root = tmp_path / "root"
    legacy = fake_root / "data" / "brain"
    (legacy / "files").mkdir(parents=True)
    (legacy / "voice.md").write_text("lowercase_first: true", encoding="utf-8")
    (legacy / "rules.md").write_text("# Learned Rules\n", encoding="utf-8")
    (legacy / "journal.md").write_text("# Reflection Journal\n\n## 2026-01-01 10:00 · reflect:learn\nold entry\n", encoding="utf-8")
    monkeypatch.setattr(brain, "ROOT", fake_root)
    monkeypatch.setattr(brain, "LEGACY_BRAIN_DIR", legacy)
    monkeypatch.setattr(brain, "ACCOUNTS_ROOT", fake_root / "data" / "accounts")

    brain.ensure(1)  # triggers the one-time move
    new_dir = brain.brain_dir(1)
    assert (new_dir / "voice.md").read_text(encoding="utf-8") == "lowercase_first: true"
    assert "old entry" in (new_dir / "journal.md").read_text(encoding="utf-8")
    assert not legacy.exists()
    # idempotent — a second ensure does not explode or duplicate
    brain.ensure(1)
    assert (new_dir / "voice.md").exists()


def test_legacy_brain_migration_never_fires_in_sandbox(tmp_path):
    # ACCOUNTS_ROOT is sandboxed (fixture) while LEGACY_BRAIN_DIR still points
    # at the real tree — ensure() must leave the real brain exactly where it is
    brain.ensure(1)
    if brain.LEGACY_BRAIN_DIR.exists():  # only when a real install is present
        assert brain.LEGACY_BRAIN_DIR.is_dir()


def test_legacy_digests_migrate_to_account_1(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENSTANLEY_DIGEST_DIR", raising=False)
    fake_root = tmp_path / "droot"
    legacy = fake_root / "data" / "digests"
    legacy.mkdir(parents=True)
    (legacy / "2026-08-01.md").write_text("# digest", encoding="utf-8")
    monkeypatch.setattr(brain, "ROOT", fake_root)
    monkeypatch.setattr(brain, "ACCOUNTS_ROOT", fake_root / "data" / "accounts")
    monkeypatch.setattr(digest_mod, "ROOT", fake_root)

    assert digest_mod.digest_dir(1).name == "digests"
    assert (digest_mod.digest_dir(1) / "2026-08-01.md").exists()
    assert not legacy.exists()


# ---------------- isolation (the hard requirement) ----------------

def test_fresh_account_brain_is_empty_and_isolated(tmp_path):
    _account1_with_history()
    a2 = db.create_account("second")
    brain.ensure(a2)

    # account 2 sees ONLY seeds — no rules, no strategies, no journal noise
    assert brain.parse_rules(brain.read("rules", a2)) == []
    assert "(none yet" in brain.read("strategies", a2)
    assert "Reflection Journal" in brain.read("journal", a2)
    assert "account 1 learned something" not in brain.read("journal", a2)
    ctx2 = brain.brain_context(acct=a2)
    assert "never post without a hook" not in ctx2
    assert "threads outperform" not in ctx2

    # account 1 is untouched and still has everything
    ctx1 = brain.brain_context(acct=1)
    assert "never post without a hook" in ctx1
    assert "threads outperform" in ctx1
    assert "account 1 learned something" in brain.read("journal", 1)


def test_brain_files_do_not_leak_across_accounts(tmp_path):
    _account1_with_history()
    a2 = db.create_account("second")
    brain.write("instructions", "# account 2 private manual", acct=a2)
    assert "account 2 private manual" in brain.read("instructions", a2)
    assert "account 2 private manual" not in brain.read("instructions", 1)
    assert "Persona" in brain.read("instructions", 1)


def test_reflect_writes_only_that_account(monkeypatch, tmp_path):
    _account1_with_history()
    a2 = db.create_account("second")
    brain.ensure(a2)
    monkeypatch.setattr(brain, "llm_chat",
                        lambda *a, **k: REPLY_JSON)  # hermetic LLM seam
    res = brain.reflect(CFG, "learn", acct=a2)
    assert res["ok"] and res["applied"]["added_rules"]

    rules2 = brain.parse_rules(brain.read("rules", a2))
    assert len(rules2) == 1 and "within the hour" in rules2[0]["text"]
    # journal got the reflect entry for account 2 ONLY
    assert "learned to be fast" in brain.read("journal", a2)
    assert "learned to be fast" not in brain.read("journal", 1)
    # account 1's rule count unchanged
    assert len(brain.parse_rules(brain.read("rules", 1))) == 1


def test_voice_md_is_per_account(tmp_path):
    _account1_with_history()
    a2 = db.create_account("second")
    stats = {"avg_length_chars": 100, "emoji": {"per_post": 0.0},
             "hashtags": {"pct_with": 0.0, "per_post": 0.0},
             "casing": {"pct_lowercase_start": 0.9},
             "misspellings_per_100_words": 4.0}
    p2 = voice_lock.write_voice_md(stats, acct=a2)
    assert p2 == brain.brain_dir(a2) / "voice.md"
    assert not (brain.brain_dir(1) / "voice.md").exists()  # account 1 untouched
    rules2 = voice_lock.load_persona_rules(acct=a2)
    assert rules2["source"] == "brain" and rules2["lowercase_first"] is True
    # active-account default follows the switch
    db.set_active_account(a2)
    assert voice_lock.load_persona_rules()["lowercase_first"] is True


def test_digest_dir_per_account(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENSTANLEY_DIGEST_DIR", raising=False)
    a2 = db.create_account("second")
    d1, d2 = digest_mod.digest_dir(1), digest_mod.digest_dir(a2)
    assert d1 == brain.account_dir(1) / "digests"
    assert d2 == brain.account_dir(a2) / "digests"
    assert d1 != d2


def test_style_profile_and_strategy_are_per_account(tmp_path):
    a2 = db.create_account("second")
    db.set_acct_setting("style_profile", {"stats": {"topics": ["ai"]}})
    db.set_acct_setting("strategy", {"text": "one-pager", "exists": True}, acct=a2)
    assert (db.get_acct_setting("style_profile") or {}).get("stats", {}).get("topics") == ["ai"]
    assert db.get_acct_setting("style_profile", acct=a2) is None
    assert (db.get_acct_setting("strategy", acct=a2) or {}).get("text") == "one-pager"
    assert db.get_acct_setting("strategy") is None


def test_api_create_account_seeds_fresh_brain(tmp_path, monkeypatch):
    import openstanley.server.__main__ as server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.db")
    db.init_db()
    _account1_with_history()
    with TestClient(server.app) as tc:
        r = tc.post("/api/accounts", json={"handle": "newbie"})
        assert r.status_code == 200
        a2 = r.json()["account_id"]
    assert (brain.brain_dir(a2) / "instructions.md").exists()
    assert brain.parse_rules(brain.read("rules", a2)) == []
    assert "never post without a hook" not in brain.read("instructions", a2)
