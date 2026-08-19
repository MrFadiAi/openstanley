"""Multi-account (v0.5.0) — account registry + DB scoping.

Phase 1 of UPGRADE_BRIEF_MULTIACCOUNT.md:
  * in-place migration of a pre-v0.5 DB → every scoped row lands in account 1
  * per-account uniqueness (the same x_id can exist for two accounts)
  * every scoped db helper filters by account (explicit acct param or the
    active-account default)
  * accounts API: list / create / activate / cookies (write-only, masked) /
    archive+delete
  * safety caps keyed per account
  * per-account identity ("me")

All hermetic: dryrun X, OPENSTANLEY_TEST_DB, no LLM seams touched.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

os.environ.setdefault("OPENSTANLEY_NO_SCHEDULER", "1")  # before importing the server

import pytest  # noqa: E402

from openstanley.core import db            # noqa: E402
db.init_db()

import openstanley.server.__main__ as server  # noqa: E402
from openstanley.core import safety        # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# the pre-v0.5 schema (v0.4.5) — what a real old install's DB looks like
OLD_SCHEMA = """
CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    x_id TEXT UNIQUE,
    author_handle TEXT,
    is_own INTEGER DEFAULT 0,
    created_at TEXT,
    text TEXT,
    impressions INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    bookmarks INTEGER DEFAULT 0,
    engagement REAL DEFAULT 0,
    topics TEXT DEFAULT '',
    raw_json TEXT
);
CREATE TABLE voice_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rubric TEXT DEFAULT '',
    examples_json TEXT DEFAULT '[]',
    updated_at TEXT
);
CREATE TABLE ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    angle TEXT,
    format TEXT DEFAULT 'one-liner',
    source TEXT,
    status TEXT DEFAULT 'new',
    score REAL DEFAULT 0,
    created_at TEXT,
    used_at TEXT
);
CREATE TABLE drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id INTEGER,
    kind TEXT DEFAULT 'post',
    text TEXT NOT NULL,
    thread_json TEXT,
    status TEXT DEFAULT 'draft',
    temperature TEXT DEFAULT 'safe',
    scheduled_at TEXT,
    x_id TEXT,
    meta_json TEXT,
    created_at TEXT,
    published_at TEXT
);
CREATE TABLE engagements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    x_id TEXT UNIQUE,
    kind TEXT,
    author_handle TEXT,
    author_name TEXT,
    text TEXT,
    draft_id INTEGER,
    status TEXT DEFAULT 'new',
    created_at TEXT,
    seen_at TEXT
);
CREATE TABLE seen_mentions (
    x_id TEXT PRIMARY KEY,
    author TEXT,
    text TEXT,
    created_at TEXT,
    first_seen TEXT,
    handled INTEGER DEFAULT 0
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    role TEXT,
    content TEXT,
    meta_json TEXT DEFAULT '{}',
    ts TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE agent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    level TEXT DEFAULT 'info',
    loop TEXT,
    message TEXT
);
CREATE TABLE eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    label TEXT DEFAULT 'manual',
    real_llm INTEGER DEFAULT 0,
    use_brain INTEGER DEFAULT 1,
    status TEXT DEFAULT 'running',
    total REAL,
    deltas_json TEXT,
    report_md TEXT,
    config_json TEXT,
    error TEXT
);
CREATE TABLE eval_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    suite TEXT,
    score REAL,
    details_json TEXT
);
CREATE TABLE metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_x_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    likes INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    impressions INTEGER DEFAULT 0
);
CREATE TABLE identity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    followers INTEGER DEFAULT 0
);
"""


def _old_db(tmp_path: Path) -> Path:
    """A pre-v0.5 database with one row in each scoped table."""
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO posts (x_id, author_handle, is_own, created_at, text) "
                 "VALUES ('x1', 'legacy_user', 1, '2026-01-01T09:00:00', 'old own post')")
    conn.execute("INSERT INTO drafts (idea_id, kind, text, status) "
                 "VALUES (NULL, 'post', 'old draft', 'draft')")
    conn.execute("INSERT INTO ideas (title, angle, source) "
                 "VALUES ('old idea', 'angle', 'scan')")
    conn.execute("INSERT INTO engagements (x_id, kind, author_handle, text) "
                 "VALUES ('e1', 'mention', 'someone', 'hi')")
    conn.execute("INSERT INTO seen_mentions (x_id, author, text) "
                 "VALUES ('m1', 'someone', 'hello')")
    conn.execute("INSERT INTO metric_snapshots (post_x_id, captured_at, likes) "
                 "VALUES ('x1', '2026-01-02T09:00:00', 5)")
    conn.execute("INSERT INTO identity_snapshots (captured_at, followers) "
                 "VALUES ('2026-01-02T09:00:00', 321)")
    conn.execute("INSERT INTO voice_profile (id, rubric, examples_json) "
                 "VALUES (1, 'legacy rubric', '[]')")
    conn.execute("INSERT INTO eval_runs (ts, label, status, total) "
                 "VALUES ('2026-01-02', 'manual', 'done', 80.0)")
    conn.execute("INSERT INTO eval_results (run_id, suite, score) VALUES (1, 'voice', 80.0)")
    conn.execute("INSERT INTO settings (key, value) VALUES ('me', ?)",
                 (json.dumps({"username": "legacy_user", "followers": 321}),))
    conn.commit()
    conn.close()
    return p


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    with TestClient(server.app) as tc:
        yield tc


@pytest.fixture(autouse=True)
def _reset_active_account():
    db.set_active_account(1)
    yield
    db.set_active_account(1)


# ---------------- migration ----------------

def test_migration_old_db_in_place(tmp_path, monkeypatch):
    old = _old_db(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", old)
    db.init_db()  # in-place migration
    with db.connect() as c:
        for table in db.SCOPED_TABLES:
            (n, bad) = c.execute(
                f"SELECT COUNT(*), SUM(account_id != 1) FROM {table}").fetchone()
            assert n > 0, f"{table} lost its rows during migration"
            assert not bad, f"{table} has rows outside account 1"
        accounts = [dict(r) for r in c.execute("SELECT * FROM accounts").fetchall()]
    assert len(accounts) == 1 and accounts[0]["id"] == 1
    assert accounts[0]["handle"] == "legacy_user"   # handle from the old "me"
    assert not accounts[0]["cookies_json"]
    # migrated data is still readable through the helpers
    assert db.own_posts()[0]["x_id"] == "x1"
    assert db.get_me()["username"] == "legacy_user"  # legacy key fallback
    assert db.drafts_by_status("draft")[0]["text"] == "old draft"
    assert db.fresh_ideas()[0]["title"] == "old idea"
    assert db.load_voice()["rubric"] == "legacy rubric"
    run = db.list_eval_runs()[0]
    assert run["status"] == "done" and run["total"] == 80.0
    # idempotent: a second init does not duplicate accounts or rows
    db.init_db()
    with db.connect() as c:
        (n,) = c.execute("SELECT COUNT(*) FROM accounts").fetchone()
        (posts_n,) = c.execute("SELECT COUNT(*) FROM posts").fetchone()
    assert n == 1 and posts_n == 1


def test_migration_idempotent_on_fresh_schema(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "DB_PATH", fresh)
    db.init_db()
    db.init_db()
    with db.connect() as c:
        (n,) = c.execute("SELECT COUNT(*) FROM accounts").fetchone()
    assert n == 1  # bootstrap account, never zero


def test_same_x_id_two_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "two.db")
    db.init_db()
    a2 = db.create_account("second")
    db.upsert_post({"x_id": "shared", "author_handle": "someone", "is_own": 1,
                    "text": "account 1 copy"}, acct=1)
    db.upsert_post({"x_id": "shared", "author_handle": "someone", "is_own": 1,
                    "text": "account 2 copy"}, acct=a2)  # no UNIQUE violation
    own1 = db.own_posts(acct=1)
    own2 = db.own_posts(acct=a2)
    assert len(own1) == 1 and len(own2) == 1
    assert own1[0]["text"] == "account 1 copy"
    assert own2[0]["text"] == "account 2 copy"


# ---------------- helper scoping ----------------

def test_scoped_helpers_filter_by_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scope.db")
    db.init_db()
    a2 = db.create_account("second")

    db.add_idea("i1", "a", "one-liner", "scan", 5.0, acct=1)
    db.add_idea("i2", "a", "one-liner", "scan", 5.0, acct=a2)
    assert [i["title"] for i in db.fresh_ideas(acct=1)] == ["i1"]
    assert [i["title"] for i in db.fresh_ideas(acct=a2)] == ["i2"]
    assert db.idea_count(acct=1) == 1 and db.idea_count(acct=a2) == 1

    d1 = db.add_draft("d1 text", acct=1)
    d2 = db.add_draft("d2 text", acct=a2)
    assert [d["text"] for d in db.drafts_by_status("draft", acct=1)] == ["d1 text"]
    assert [d["text"] for d in db.drafts_by_status("draft", acct=a2)] == ["d2 text"]
    assert db.get_draft(d2, acct=1) is None        # cross-account read refused
    assert db.get_draft(d1, acct=a2) is None
    assert (db.get_draft(d1, acct=1) or {})["text"] == "d1 text"
    db.update_draft(d2, acct=1, status="approved")  # wrong account → no-op
    assert db.get_draft(d2, acct=a2)["status"] == "draft"
    db.update_draft(d2, acct=a2, status="approved")
    assert db.get_draft(d2, acct=a2)["status"] == "approved"

    db.save_voice("r1", [{"t": "e1"}], acct=1)
    db.save_voice("r2", [{"t": "e2"}], acct=a2)
    assert db.load_voice(acct=1)["rubric"] == "r1"
    assert db.load_voice(acct=a2)["rubric"] == "r2"
    assert db.load_voice(acct=99) is None

    s1 = db.add_eval_run(label="manual", acct=1)
    db.add_eval_result(s1, "voice", 70.0, acct=1)
    assert db.get_eval_run(s1, acct=a2) is None
    assert db.list_eval_runs(acct=a2) == []
    assert db.list_eval_runs(acct=1)[0]["id"] == s1


def test_active_account_switch(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "act.db")
    db.init_db()
    assert db.active_account() == 1
    a2 = db.create_account("second")
    assert db.set_active_account(a2) is True
    assert db.active_account() == a2
    assert db.set_active_account(999) is False    # unknown id refused
    assert db.active_account() == a2
    # implicit scoping follows the active account
    db.add_idea("active-side", "a", "one-liner", "scan", 5.0)
    assert db.fresh_ideas(acct=a2)[0]["title"] == "active-side"
    assert db.fresh_ideas(acct=1) == []


def test_me_helpers_per_account(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "me.db")
    db.init_db()
    a2 = db.create_account("second")
    db.set_me({"username": "first", "followers": 10}, acct=1)
    db.set_me({"username": "second", "followers": 20}, acct=a2)
    assert db.get_me(acct=1)["username"] == "first"
    assert db.get_me(acct=a2)["username"] == "second"
    assert db.get_me(acct=99) == {}
    assert db.get_setting("me")["username"] == "first"  # legacy mirror for acct 1


def test_safety_counters_per_account(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "safety.db")
    db.init_db()
    a2 = db.create_account("second")
    caps = {"max_posts_per_day": 1, "max_replies_per_day": 10}
    safety.check_and_record("posts", caps, acct=1)
    with pytest.raises(safety.SafetyCapExceeded):
        safety.check_and_record("posts", caps, acct=1)   # account 1 capped
    safety.check_and_record("posts", caps, acct=a2)      # account 2 unaffected
    assert safety.usage(acct=1)["posts"] == 1
    assert safety.usage(acct=a2)["posts"] == 1


# ---------------- API ----------------

def test_api_accounts_crud(client, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.db")
    db.init_db()
    r = client.get("/api/accounts")
    assert r.status_code == 200
    body = r.json()
    assert body["active_account_id"] == 1
    assert [a["id"] for a in body["accounts"]] == [1]

    r = client.post("/api/accounts", json={"handle": "@newbie"})
    assert r.status_code == 200 and r.json()["handle"] == "newbie"
    a2 = r.json()["account_id"]

    r = client.post("/api/accounts", json={"handle": ""})
    assert r.status_code == 400

    # activate + the registry marks it active
    r = client.post(f"/api/accounts/{a2}/activate")
    assert r.status_code == 200 and r.json()["active_account_id"] == a2
    assert db.active_account() == a2
    accounts = client.get("/api/accounts").json()["accounts"]
    assert next(a for a in accounts if a["id"] == a2)["active"] is True
    assert next(a for a in accounts if a["id"] == 1)["active"] is False

    r = client.post("/api/accounts/999/activate")
    assert r.status_code == 404


def test_api_account_cookies_write_only(client, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cookies.db")
    db.init_db()
    cookies = json.dumps({"auth_token": "tok-1234567890", "ct0": "ct0-value"})
    r = client.post("/api/accounts/1/cookies", json={"cookies_json": cookies})
    assert r.status_code == 200
    assert r.json()["cookies_masked"] == "••••7890"
    # GET views never carry values — only set-flag + masked hint
    accounts = client.get("/api/accounts").json()["accounts"]
    me_row = next(a for a in accounts if a["id"] == 1)
    assert me_row["cookies_set"] is True
    assert me_row["cookies_masked"] == "••••7890"
    assert "auth_token" not in json.dumps(accounts)
    # bad payloads rejected
    r = client.post("/api/accounts/1/cookies", json={"cookies_json": "not json"})
    assert r.status_code == 400
    r = client.post("/api/accounts/1/cookies",
                    json={"cookies_json": json.dumps({"ct0": "x"})})
    assert r.status_code == 400


def test_api_delete_account_archives(client, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "del.db")
    db.init_db()
    db.upsert_post({"x_id": "p1", "author_handle": "gone", "is_own": 1, "text": "x"},
                   acct=1)
    a2 = db.create_account("second")
    # deleting the only other account is fine; deleting the LAST one is not
    r = client.delete(f"/api/accounts/{a2}")
    assert r.status_code == 200
    arch = Path(r.json()["archived_to"])
    assert (arch / "dump.json").exists()
    r = client.delete("/api/accounts/1")
    assert r.status_code == 409
    assert db.get_account(a2) is None
    assert db.own_posts(acct=1)  # untouched


def test_api_delete_active_account_reassigns(client, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "del2.db")
    db.init_db()
    db.add_idea("only-in-1", "a", "one-liner", "scan", 5.0, acct=1)
    a2 = db.create_account("second")
    client.post(f"/api/accounts/{a2}/activate")
    r = client.delete(f"/api/accounts/{a2}")  # delete the ACTIVE one
    assert r.status_code == 200
    assert db.active_account() == 1            # fell back to a remaining account
    assert db.idea_count(acct=1) == 1


def test_x_status_scoped_to_active_account(client, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "xstatus.db")
    monkeypatch.setenv(server.cfg.x.cookies_env, "")  # kill the .env bootstrap leak
    db.init_db()
    db.set_me({"username": "first", "followers": 10}, acct=1)
    r = client.get("/api/x/status")
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == 1
    assert body["username"] == "first"
    assert body["cookies_set"] is False
    assert body["cookies_masked"] is None
