"""The Brain v0.3.2 — structure, sanitization, reflect(), prompt injection, API.

All LLM calls are faked; nothing touches the network. The brain directory is
sandboxed into a pytest tmp dir so the real data/brain/ is never touched.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ["OPENSTANLEY_NO_SCHEDULER"] = "1"  # before importing the server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

from openstanley.core import db               # noqa: E402
db.init_db()

import openstanley.server.__main__ as server  # noqa: E402

client = TestClient(server.app)

from openstanley.core.config import Config    # noqa: E402
from openstanley.gen import brain             # noqa: E402
from openstanley.gen import chat as chat_mod  # noqa: E402
from openstanley.gen import drafts as drafts_mod  # noqa: E402

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082")


@pytest.fixture(autouse=True)
def _brain_sandbox(tmp_path, monkeypatch):
    """Every test in this module runs against a fresh sandboxed brain dir."""
    sandbox = tmp_path / "brain"
    # v0.5.0: brains live under ACCOUNTS_ROOT/<id>/brain — sandbox the anchor
    monkeypatch.setattr(brain, "ACCOUNTS_ROOT", tmp_path / "accounts")
    sandbox = brain.brain_dir()
    brain.ensure()
    yield


def _reset_chat_counter():
    db.set_setting("brain_chat_counter", 0)
    db.set_setting("brain_last_chat_id", 0)


# ---------- structure + read/write ----------

def test_brain_seeds_structure():
    for part in ("instructions", "rules", "strategies", "journal"):
        assert brain.read(part).strip(), part  # seeded, non-empty md
    assert "Persona" in brain.read("instructions")
    for stem in brain.SEED_FILES:
        text = brain.read(f"files/{stem}")
        assert text.strip(), stem
    assert "Persona" in brain.read("instructions")  # seeded in OpenStanley's voice
    inv = brain.inventory()
    names = [p["name"] for p in inv]
    assert names[0] == "instructions" and "photos" in names
    assert len(names) == 4 + len(brain.SEED_FILES) + 1
    for p in inv:
        assert p["type"] in ("md", "photos") and p["size"] >= 0
        assert p["summary"]


def test_read_write_roundtrip():
    brain.write("instructions", "# My manual\n- be brief")
    assert "be brief" in brain.read("instructions")
    brain.write("files/niche-map", "# niche\n- ai agents")
    assert "ai agents" in brain.read("files/niche-map")
    with pytest.raises(FileNotFoundError):
        brain.read("files/../../etc/passwd")
    with pytest.raises(FileNotFoundError):
        brain.read("unknown-part")


# ---------- sanitization ----------

@pytest.mark.parametrize("bad", [
    "OPENSTANLEY_LLM_API_KEY=abcdef1234567890abcdef",
    "set api_key: sk-1234567890abcdefghijklmnopqrst",
    "auth_token: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    '{"cookies": "3f4a9c7d8e2b1a0f9e8d7c6b5a4f3e2d1c0b9a8f7e6d"}',
    "bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.abc",
    "ct0=8842aabbccddeeff0011223344556677",
    "PASSWORD=supersecret42",
])
def test_sanitization_rejects_secrets(bad):
    for writer in (lambda: brain.write("instructions", bad),
                   lambda: brain.add_rule(bad, "chat")):
        with pytest.raises(brain.BrainSecurityError):
            writer()


@pytest.mark.parametrize("good", [
    "Never post without explicit user approval.",
    "Reply in Arabic when the user writes Arabic — including ؟ ، ؛",
    "9pm scheduling is fine when the user asks for it.",
    "The key insight: ship the ugly version first.",
])
def test_sanitization_accepts_normal_text(good):
    brain.write("instructions", good)  # must not raise
    rid = brain.add_rule(good, "chat")
    assert rid >= 1


# ---------- reflect() ----------

FAKE_REFLECT_JSON = {
    "instructions_delta": "User prefers direct answers without preamble.",
    "new_rules": [
        {"text": "Never use hashtags — the account never uses them", "why": "user correction"},
        {"text": "Arabic posts always use ؟ ، ؛ punctuation", "why": "convention fix"},
    ],
    "retire_rule_ids": [1],
    "strategy_updates": [
        {"title": "Question-endings drive replies", "note": "3 of top 5 posts end with ?",
         "outcome": "working"},
    ],
    "journal_entry": "User corrected hashtags twice; locked it in as a rule.",
}


def test_reflect_applies_edits():
    rid1 = brain.add_rule("Old rule: post mornings only", "learn")
    assert rid1 == 1

    calls = []

    def fake_llm(cfg, system, user, temperature=None, json_mode=False, retries=2):
        calls.append((system, user))
        import json as _json
        return _json.dumps(FAKE_REFLECT_JSON)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(brain, "llm_chat", fake_llm)
    try:
        res = brain.reflect(Config(), "chat")
    finally:
        monkey.undo()

    assert res["ok"] and res["trigger"] == "chat"
    applied = res["applied"]
    assert applied["added_rules"] == [2, 3]
    assert applied["retired_rules"] == [1]
    assert len(applied["strategy_updates"]) == 1
    assert applied["instructions_updated"] is True

    rules = brain.parse_rules(brain.read("rules"))
    by_id = {r["id"]: r for r in rules}
    assert by_id[1]["status"] == "retired"
    assert by_id[2]["status"] == "active"
    assert "hashtags" in by_id[2]["text"]
    assert by_id[3]["source"] == "chat"

    assert "Question-endings drive replies" in brain.read("strategies")
    assert "direct answers without preamble" in brain.read("instructions")

    journal = brain.parse_journal(brain.read("journal"))
    assert journal[0]["trigger"] == "reflect:chat"
    assert any("added R2" in c for c in journal[0]["changes"])
    assert any("retired R1" in c for c in journal[0]["changes"])

    # the reflect prompt carried the current rules + material
    assert "R1:" in calls[0][1]
    assert "chat reflect" in calls[0][0].lower() or "reflecting" in calls[0][0].lower()


def test_reflect_drops_tainted_proposals():
    import json as _json
    tainted = {
        "instructions_delta": "",
        "new_rules": [{"text": "use api_key=abcdef1234567890abcdef in prompts", "why": "-"}],
        "retire_rule_ids": [],
        "strategy_updates": [],
        "journal_entry": "nothing useful",
    }

    monkey = pytest.MonkeyPatch()
    monkey.setattr(brain, "llm_chat",
                   lambda *a, **k: _json.dumps(tainted))
    try:
        res = brain.reflect(Config(), "scan")
    finally:
        monkey.undo()
    assert res["ok"]
    assert res["applied"]["added_rules"] == []
    assert res["applied"]["dropped_tainted"] == 1
    assert brain.parse_rules(brain.read("rules")) == []  # nothing stored


def test_reflect_rejects_unknown_trigger():
    with pytest.raises(ValueError):
        brain.reflect(Config(), "bogus")


# ---------- brain_context ----------

def test_brain_context_budget_and_rules():
    for i in range(12):
        brain.add_rule(f"Rule number {i}: always ship the ugly version first", "chat")
    ctx = brain.brain_context()
    assert ctx.startswith(brain.BRAIN_HEADER)
    assert len(ctx) <= brain.BUDGET_CHARS
    assert "R1:" in ctx and "RULES" in ctx
    # tight budgets are respected too
    assert len(brain.brain_context(budget=300)) <= 300


# ---------- prompt integration ----------

def test_chat_prompt_receives_brain():
    captured = []

    def fake_llm(cfg, system, user, temperature=None, json_mode=False, retries=2):
        captured.append(system)
        return "On it — here is a quick plan."

    monkey = pytest.MonkeyPatch()
    monkey.setattr(chat_mod, "llm_chat", fake_llm)
    monkey.setattr(chat_mod, "_followup", lambda *a, **k: "")
    try:
        result = chat_mod.chat_reply(Config(), "what should I post this week?")
    finally:
        monkey.undo()
    assert captured and captured[0].startswith(brain.BRAIN_HEADER)


def test_drafts_prompt_receives_brain():
    captured = []

    def fake_llm(cfg, system, user, temperature=None, json_mode=False, retries=2):
        import json as _json
        captured.append(system)
        return _json.dumps({"tweet": "the ugly version teaches you things slides never will"})

    monkey = pytest.MonkeyPatch()
    monkey.setattr(drafts_mod, "chat", fake_llm)
    try:
        out = drafts_mod._draft_one(
            Config(), {"title": "t", "angle": "a", "format": "one-liner"}, "safe")
    finally:
        monkey.undo()
    assert out["text"]
    assert captured and brain.BRAIN_HEADER in captured[0]


def test_chat_reflect_hook_every_10th():
    _reset_chat_counter()
    fired = threading.Event()
    got: list[str] = []

    def fake_reflect(cfg, trigger):
        got.append(trigger)
        fired.set()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(brain, "reflect", fake_reflect)
    try:
        for i in range(9):
            assert brain.maybe_reflect_chat_async(Config()) is False
        assert not fired.is_set()
        assert brain.maybe_reflect_chat_async(Config()) is True
        assert fired.wait(timeout=5), "reflect thread never ran"
    finally:
        monkey.undo()
    assert got == ["chat"]
    assert db.get_setting("brain_chat_counter") == 10


# ---------- API ----------

def test_api_brain_endpoints():
    # inventory
    r = client.get("/api/brain")
    assert r.status_code == 200
    parts = r.json()["parts"]
    assert any(p["name"] == "instructions" and p["type"] == "md" for p in parts)

    # read one part (raw + parsed rules)
    brain.add_rule("api rule one", "chat")
    r = client.get("/api/brain/rules")
    assert r.status_code == 200
    body = r.json()
    assert "api rule one" in body["content"]
    assert body["rules"][0]["id"] == 1 and body["rules"][0]["status"] == "active"

    # user manual edit → 200 + journaled
    r = client.put("/api/brain/instructions", json={"content": "# manual v2\n- be terse"})
    assert r.status_code == 200
    assert "manual v2" in brain.read("instructions")
    entries = brain.parse_journal(brain.read("journal"))
    assert entries[0]["trigger"] == "user-edit:instructions"

    # secret edit → 400, nothing stored
    r = client.put("/api/brain/instructions",
                   json={"content": "api_key=abcdef1234567890abcdef"})
    assert r.status_code == 400
    assert "manual v2" in brain.read("instructions")

    # journal endpoint
    r = client.get("/api/brain/journal")
    assert r.status_code == 200 and r.json()["entries"]

    # unknown part
    assert client.get("/api/brain/nope").status_code == 404


def test_api_brain_reflect():
    import json as _json

    monkey = pytest.MonkeyPatch()
    monkey.setattr(brain, "llm_chat",
                   lambda *a, **k: _json.dumps(FAKE_REFLECT_JSON))
    try:
        r = client.post("/api/brain/reflect", json={"trigger": "chat"})
    finally:
        monkey.undo()
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["applied"]["added_rules"]
    assert client.post("/api/brain/reflect", json={"trigger": "nope"}).status_code == 400


def test_api_brain_photos():
    r = client.post("/api/brain/photos",
                    files={"file": ("growth.png", PNG_1PX, "image/png")},
                    data={"caption": "growth chart from last month"})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["ok"] and rec["name"].endswith(".png")

    # listed with caption (GLM has no vision — caption comes from the user)
    listing = client.get("/api/brain/photos").json()
    assert any(p["name"] == rec["name"] and p["caption"] == "growth chart from last month"
               for p in listing["photos"])

    # served back byte-identical
    served = client.get(f"/api/brain/photos/{rec['name']}")
    assert served.status_code == 200 and served.content == PNG_1PX

    # traversal + missing + bad type blocked
    assert client.get("/api/brain/photos/..%2F..%2Fconfig.toml").status_code in (400, 404)
    assert client.get("/api/brain/photos/missing.png").status_code == 404
    bad = client.post("/api/brain/photos",
                      files={"file": ("x.txt", b"hello", "text/plain")})
    assert bad.status_code == 400


# ---------- brain snapshot helpers (used by the harness A/B mode) ----------

def test_snapshot_roundtrip():
    brain.add_rule("snapshot rule", "learn")
    snap = brain.to_dict()
    assert "snapshot rule" in snap["rules"]
    assert brain.has_meaningful_brain() is True
    # a fresh sandbox with only seeds is not "meaningful"
    fresh = brain.to_dict()  # sanity: structure present
    assert fresh["files"]["content-pillars.md"]


# ---------- reflect("scan") updates the niche/persona reference docs ----------

def _fake_reflect_returning(payload: dict):
    import json as _json
    return lambda *a, **k: _json.dumps(payload)


SCAN_PROFILE = {
    "stats": {
        "posts_scanned": 24, "avg_length_chars": 132.5,
        "language_mix": {"ar": 0.25, "en": 0.75},
        "posting_times": {"best_hours": [9, 18]},
        "vocabulary": {"top_terms": ["agents", "taste", "shipping", "llms"]},
    },
    "human_summary": "measured voice", "updated_at": "2026-08-19T00:00:00",
}


def test_reflect_scan_updates_niche_and_persona_files():
    db.set_setting("style_profile", SCAN_PROFILE)
    payload = {
        "instructions_delta": "",
        "new_rules": [],
        "retire_rule_ids": [],
        "strategy_updates": [],
        # LLM proposes a niche-map rewrite but nothing for audience-personas
        "file_updates": [{
            "file": "niche-map",
            "content": "# Niche Map\n- builders shipping with ai agents",
        }],
        "journal_entry": "scan absorbed into the niche map",
    }
    monkey = pytest.MonkeyPatch()
    monkey.setattr(brain, "llm_chat", _fake_reflect_returning(payload))
    try:
        res = brain.reflect(Config(), "scan")
    finally:
        monkey.undo()

    assert res["applied"]["file_updates"][:2] == ["niche-map", "audience-personas"]
    assert "builders shipping with ai agents" in brain.read("files/niche-map")
    # deterministic fallback filled the personas doc from scan stats
    personas = brain.read("files/audience-personas")
    assert "scan-derived" in personas and "9:00" in personas
    # journaled with WHY
    entries = brain.parse_journal(brain.read("journal"))
    assert entries[0]["trigger"] == "reflect:scan"
    assert any(c.startswith("file niche-map") for c in entries[0]["changes"])
    assert any(c.startswith("file audience-personas") for c in entries[0]["changes"])


def test_reflect_scan_fallback_when_llm_proposes_no_files():
    db.set_setting("style_profile", SCAN_PROFILE)
    payload = {"instructions_delta": "", "new_rules": [], "retire_rule_ids": [],
               "strategy_updates": [], "journal_entry": "nothing to add"}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(brain, "llm_chat", _fake_reflect_returning(payload))
    try:
        res = brain.reflect(Config(), "scan")
    finally:
        monkey.undo()
    # both docs still refreshed deterministically
    assert {"niche-map", "audience-personas"} <= set(res["applied"]["file_updates"])
    assert "agents" in brain.read("files/niche-map")


def test_reflect_file_updates_restricted_to_seed_files():
    payload = {
        "instructions_delta": "", "new_rules": [], "retire_rule_ids": [],
        "strategy_updates": [],
        "file_updates": [
            {"file": "journal", "content": "attempted overwrite"},      # not a seed file
            {"file": "../rules", "content": "traversal"},               # rejected by resolver
        ],
        "journal_entry": "ignored",
    }
    monkey = pytest.MonkeyPatch()
    monkey.setattr(brain, "llm_chat", _fake_reflect_returning(payload))
    try:
        res = brain.reflect(Config(), "chat")
    finally:
        monkey.undo()
    assert res["applied"]["file_updates"] == []  # nothing outside files/ is writable


def test_reflect_learn_from_synthetic_metrics():
    # one post over-performs 5x, one flops
    # engagement = (likes + 3·reposts + 8·replies) / impressions — the hot
    # post rates ~0.215 (≫ the db's dry-run baseline ~0.07), the flop ~0.0002.
    # created_at = newest (own_posts keeps only the newest 60).
    from datetime import datetime, timedelta
    import secrets as _secrets
    run = _secrets.token_hex(4)  # unique rows per run — upsert never bumps created_at
    now = datetime.now()
    for i, (txt, imp, likes, reposts, replies) in enumerate([
            ("hot take about agents", 20000, 500, 200, 400),
            ("boring corporate post", 30000, 2, 0, 0)]):
        db.upsert_post({
            "x_id": f"lp-{run}-{i}", "author_handle": "u", "is_own": 1,
            "created_at": (now - timedelta(minutes=i)).isoformat(timespec="seconds"),
            "text": txt, "impressions": imp, "likes": likes,
            "reposts": reposts, "replies": replies, "bookmarks": 10,
        })
    seen = {}

    def fake_llm(cfg, system, user, temperature=None, json_mode=False, retries=2):
        seen["user"] = user
        import json as _json
        return _json.dumps({
            "instructions_delta": "",
            "new_rules": [{"text": "Lead with contrarian agent takes", "why": "5x baseline"}],
            "retire_rule_ids": [],
            "strategy_updates": [{"title": "Agent hot takes", "note": "5x baseline engagement",
                                  "outcome": "working"}],
            "journal_entry": "hot takes win on this account",
        })

    monkey = pytest.MonkeyPatch()
    monkey.setattr(brain, "llm_chat", fake_llm)
    try:
        res = brain.reflect(Config(), "learn")
    finally:
        monkey.undo()
    assert res["applied"]["added_rules"] and res["applied"]["strategy_updates"]
    # the material carried the real over/under-performers
    assert "OVER-PERFORMED" in seen["user"] and "hot take about agents" in seen["user"]
    assert "UNDER-PERFORMED" in seen["user"] and "boring corporate post" in seen["user"]
    assert "Agent hot takes" in brain.read("strategies")


def test_brain_context_ab_toggle():
    brain.add_rule("End posts with a question when it fits", "chat")
    token = brain.set_brain_enabled(False)  # harness B arm: brain off
    try:
        assert brain.brain_context() == ""
    finally:
        brain.set_brain_enabled(True)
        # token reset restores thread-local default
    assert brain.brain_context().startswith(brain.BRAIN_HEADER)


def test_atomic_write_survives_windows_lock_race(monkeypatch):
    """WinError 5 on os.replace (AV/indexer holding the destination, live
    2026-08-28) must retry, not lose the write."""
    import pathlib
    import tempfile
    from openstanley.gen import brain
    calls = {"n": 0}
    real_replace = pathlib.Path.replace

    def flaky_replace(self, target):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return real_replace(self, target)
    monkeypatch.setattr(pathlib.Path, "replace", flaky_replace)
    monkeypatch.setattr(brain._time, "sleep", lambda s: None) if hasattr(brain, "_time") else None
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "rules.md"
        brain._atomic_write(p, "content survives the race")
        assert p.read_text(encoding="utf-8") == "content survives the race"
        assert calls["n"] == 3
