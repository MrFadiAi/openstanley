"""SQLite persistence layer — all state lives in data/openstanley.db."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(os.environ.get("OPENSTANLEY_TEST_DB")
               or (Path(__file__).resolve().parent.parent.parent / "data" / "openstanley.db"))
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
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
    raw_json TEXT,
    metrics_json TEXT                   -- LATEST metrics capture (history lives in metric_snapshots)
);
CREATE INDEX IF NOT EXISTS idx_posts_own ON posts(is_own, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_handle);

-- append-only time series (v0.3.6 analytics ground truth — never rewritten)
CREATE TABLE IF NOT EXISTS metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_x_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    likes INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    impressions INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_metric_snap_post ON metric_snapshots(post_x_id, captured_at);

CREATE TABLE IF NOT EXISTS identity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    followers INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_identity_snap_time ON identity_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS voice_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rubric TEXT DEFAULT '',
    examples_json TEXT DEFAULT '[]',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    angle TEXT,
    format TEXT DEFAULT 'one-liner',
    source TEXT,
    status TEXT DEFAULT 'new',   -- new|drafted|used|discarded
    score REAL DEFAULT 0,
    created_at TEXT,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id INTEGER,
    kind TEXT DEFAULT 'post',        -- post|reply
    text TEXT NOT NULL,
    thread_json TEXT,                -- JSON list for threads, null for single
    status TEXT DEFAULT 'draft',     -- draft|approved|rejected|published|failed
    temperature TEXT DEFAULT 'safe', -- safe|bold|experimental
    scheduled_at TEXT,
    x_id TEXT,
    meta_json TEXT,
    created_at TEXT,
    published_at TEXT,
    FOREIGN KEY (idea_id) REFERENCES ideas(id)
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

CREATE TABLE IF NOT EXISTS engagements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    x_id TEXT UNIQUE,
    kind TEXT,                        -- mention|reply-to-me|notification
    author_handle TEXT,
    author_name TEXT,
    text TEXT,
    draft_id INTEGER,
    status TEXT DEFAULT 'new',        -- new|drafted|replied|ignored
    created_at TEXT,
    seen_at TEXT
);

-- mention inbox (v0.3.9): every incoming @-mention, deduped by x_id;
-- handled=1 exactly when a reply draft exists for it
CREATE TABLE IF NOT EXISTS seen_mentions (
    x_id TEXT PRIMARY KEY,
    author TEXT,
    text TEXT,
    created_at TEXT,
    first_seen TEXT,
    handled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,                  -- NULL = dashboard chat; set = Telegram chat id
    role TEXT,                        -- user|assistant
    content TEXT,
    meta_json TEXT DEFAULT '{}',
    ts TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS agent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    level TEXT DEFAULT 'info',
    loop TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    label TEXT DEFAULT 'manual',      -- manual | ab:with-brain | ab:no-brain
    real_llm INTEGER DEFAULT 0,
    use_brain INTEGER DEFAULT 1,
    status TEXT DEFAULT 'running',    -- running | done | error
    total REAL,
    deltas_json TEXT,                 -- per-suite delta vs previous run
    report_md TEXT,
    config_json TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    suite TEXT,
    score REAL,
    details_json TEXT,
    FOREIGN KEY (run_id) REFERENCES eval_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _lock, connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    """Idempotent column migrations for DBs created before v0.3."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(drafts)").fetchall()}
    if "image" not in cols:
        c.execute("ALTER TABLE drafts ADD COLUMN image TEXT")
    if "quote_of" not in cols:
        c.execute("ALTER TABLE drafts ADD COLUMN quote_of TEXT")  # quoted tweet x_id
    pcols = {r["name"] for r in c.execute("PRAGMA table_info(posts)").fetchall()}
    if "metrics_json" not in pcols:
        c.execute("ALTER TABLE posts ADD COLUMN metrics_json TEXT")
    # v0.4.5: Telegram chat turns are persisted alongside the dashboard chat —
    # chat_id NULL = dashboard, set = that TG chat's id (histories never mix)
    ccols = {r["name"] for r in c.execute("PRAGMA table_info(chat_messages)").fetchall()}
    if "chat_id" not in ccols:
        c.execute("ALTER TABLE chat_messages ADD COLUMN chat_id INTEGER")


def log(loop: str, message: str, level: str = "info") -> None:
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO agent_log (ts, level, loop, message) VALUES (?,?,?,?)",
            (_now(), level, loop, message),
        )


def get_setting(key: str, default: Any = None) -> Any:
    with _lock, connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


# ---------- chat (OpenStanley agent) ----------

def add_chat_message(role: str, content: str, meta: dict | None = None,
                     chat_id: int | None = None) -> int:
    """chat_id NULL = the dashboard chat; set = that Telegram chat's id."""
    with _lock, connect() as c:
        cur = c.execute(
            "INSERT INTO chat_messages(chat_id, role, content, meta_json) "
            "VALUES (?,?,?,?)",
            (chat_id, role, content, json.dumps(meta or {})),
        )
        return cur.lastrowid


def chat_history(limit: int = 40) -> list[dict]:
    """Dashboard chat history — Telegram turns (chat_id set) are excluded so
    the two frontends never mix histories."""
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT id, role, content, ts, meta_json FROM chat_messages "
            "WHERE chat_id IS NULL ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    out = []
    for r in reversed(rows):
        out.append({
            "id": r["id"], "role": r["role"], "content": r["content"],
            "ts": r["ts"], "meta": json.loads(r["meta_json"] or "{}"),
        })
    return out


def chat_history_for_chat(chat_id: int, limit: int = 40) -> list[dict]:
    """One Telegram chat's persisted turns, oldest → newest."""
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT id, role, content, ts, meta_json FROM chat_messages "
            "WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit),
        ).fetchall()
    out = []
    for r in reversed(rows):
        out.append({
            "id": r["id"], "role": r["role"], "content": r["content"],
            "ts": r["ts"], "meta": json.loads(r["meta_json"] or "{}"),
        })
    return out


# ---------- posts ----------

def upsert_post(p: dict) -> None:
    # Algorithm-weighted engagement (X 2025-26 weights: reply ≈27-75x like, RT ≈2x like).
    # Pragmatic skew 1:3:8 (likes:reposts:replies) — full 1:2:54 ratio overweights
    # single-viral-reply posts for ranking a personal corpus.
    eng = float(p.get("likes", 0)) + 3 * float(p.get("reposts", 0)) + 8 * float(p.get("replies", 0))
    impressions = float(p.get("impressions") or 0)
    rate = (eng / impressions) if impressions > 0 else 0.0
    with _lock, connect() as c:
        c.execute(
            """INSERT INTO posts (x_id, author_handle, is_own, created_at, text,
               impressions, likes, reposts, replies, bookmarks, engagement, topics, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(x_id) DO UPDATE SET
                 impressions=excluded.impressions, likes=excluded.likes,
                 reposts=excluded.reposts, replies=excluded.replies,
                 bookmarks=excluded.bookmarks, engagement=excluded.engagement""",
            (
                p.get("x_id"), p.get("author_handle", ""), int(p.get("is_own", 0)),
                p.get("created_at"), p.get("text", ""),
                int(p.get("impressions", 0)), int(p.get("likes", 0)),
                int(p.get("reposts", 0)), int(p.get("replies", 0)),
                int(p.get("bookmarks", 0)), round(rate, 5),
                json.dumps(p.get("topics", [])), json.dumps(p.get("raw", {})),
            ),
        )


def own_posts(limit: int = 500) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE is_own=1 ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def niche_posts(limit: int = 400) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE is_own=0 AND engagement > 0 "
            "ORDER BY engagement DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def posts_needing_metrics(hours: int = 72, limit: int = 100) -> list[dict]:
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE is_own=1 AND created_at > ? AND x_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?", (cutoff, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- voice ----------

def save_voice(rubric: str, examples: list[dict]) -> None:
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO voice_profile (id, rubric, examples_json, updated_at) VALUES (1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET rubric=excluded.rubric, "
            "examples_json=excluded.examples_json, updated_at=excluded.updated_at",
            (rubric, json.dumps(examples, ensure_ascii=False), _now()),
        )


def load_voice() -> Optional[dict]:
    with _lock, connect() as c:
        row = c.execute("SELECT * FROM voice_profile WHERE id=1").fetchone()
    if row is None:
        return None
    d = dict(row)
    d["examples"] = json.loads(d.pop("examples_json") or "[]")
    return d


# ---------- ideas ----------

def add_idea(title: str, angle: str, fmt: str, source: str, score: float = 0.0) -> int:
    with _lock, connect() as c:
        cur = c.execute(
            "INSERT INTO ideas (title, angle, format, source, score, created_at) VALUES (?,?,?,?,?,?)",
            (title, angle, fmt, source, score, _now()),
        )
        return cur.lastrowid


def fresh_ideas(limit: int = 20) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM ideas WHERE status='new' ORDER BY score DESC, created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def idea_count() -> int:
    with _lock, connect() as c:
        (n,) = c.execute("SELECT COUNT(*) FROM ideas WHERE status='new'").fetchone()
    return n


def mark_idea(idea_id: int, status: str) -> None:
    with _lock, connect() as c:
        c.execute(
            "UPDATE ideas SET status=?, used_at=CASE WHEN ?='used' THEN ? ELSE used_at END WHERE id=?",
            (status, status, _now(), idea_id),
        )


# ---------- drafts ----------

def add_draft(text: str, idea_id: Optional[int] = None, kind: str = "post",
              thread: Optional[list[str]] = None, temperature: str = "safe",
              meta: Optional[dict] = None, image: Optional[str] = None,
              quote_of: Optional[str] = None,
              scheduled_at: Optional[str] = None,
              status: str = "draft") -> int:
    with _lock, connect() as c:
        cur = c.execute(
            "INSERT INTO drafts (idea_id, kind, text, thread_json, status, temperature, meta_json, image, quote_of, scheduled_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (idea_id, kind, text, json.dumps(thread) if thread else None,
             status, temperature, json.dumps(meta or {}, ensure_ascii=False),
             image, quote_of, scheduled_at, _now()),
        )
        return cur.lastrowid


def update_draft(draft_id: int, **fields: Any) -> None:
    allowed = {"text", "thread_json", "status", "scheduled_at", "x_id", "published_at",
               "meta_json", "image", "quote_of", "kind", "temperature"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("thread_json", "meta_json") and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    vals.append(draft_id)
    with _lock, connect() as c:
        c.execute(f"UPDATE drafts SET {', '.join(sets)} WHERE id=?", vals)


def drafts_by_status(status: str, limit: int = 100) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM drafts WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["thread"] = json.loads(d.pop("thread_json") or "null")
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        out.append(d)
    return out


def next_scheduled() -> Optional[dict]:
    with _lock, connect() as c:
        row = c.execute(
            "SELECT * FROM drafts WHERE status='approved' AND scheduled_at IS NOT NULL "
            "AND scheduled_at <= ? ORDER BY scheduled_at LIMIT 1",
            (_now(),),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["thread"] = json.loads(d.pop("thread_json") or "null")
    d["meta"] = json.loads(d.pop("meta_json") or "{}")
    return d


def get_draft(draft_id: int) -> Optional[dict]:
    """Fetch one draft of any status, with parsed thread/meta."""
    with _lock, connect() as c:
        row = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["thread"] = json.loads(d.pop("thread_json") or "null")
    d["meta"] = json.loads(d.pop("meta_json") or "{}")
    return d


def dashboard_stats() -> dict:
    with _lock, connect() as c:
        counts = {}
        for status in ("draft", "approved", "published", "rejected", "failed"):
            (counts[status],) = c.execute(
                "SELECT COUNT(*) FROM drafts WHERE status=? AND kind='post'", (status,)
            ).fetchone()
        (own_count,) = c.execute("SELECT COUNT(*) FROM posts WHERE is_own=1").fetchone()
        (niche_count,) = c.execute("SELECT COUNT(*) FROM posts WHERE is_own=0").fetchone()
        (eng_new,) = c.execute("SELECT COUNT(*) FROM engagements WHERE status='new'").fetchone()
    return {
        "drafts": counts, "own_posts": own_count, "niche_posts": niche_count,
        "new_engagements": eng_new, "ideas_bank": idea_count(),
    }


# ---------- harness (eval runs) ----------

def add_eval_run(label: str = "manual", real_llm: bool = False,
                 use_brain: bool = True, config: dict | None = None) -> int:
    with _lock, connect() as c:
        cur = c.execute(
            "INSERT INTO eval_runs (ts, label, real_llm, use_brain, status, config_json) "
            "VALUES (?,?,?,?, 'running', ?)",
            (_now(), label, int(real_llm), int(use_brain),
             json.dumps(config or {}, ensure_ascii=False)),
        )
        return cur.lastrowid


def update_eval_run(run_id: int, **fields: Any) -> None:
    allowed = {"status", "total", "deltas_json", "report_md", "error"}
    sets, vals = [], []
    for k, v in fields.items():
        if k == "deltas":  # alias used by report.finalize
            k = "deltas_json"
        if k not in allowed:
            continue
        if k == "deltas_json" and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False, default=str)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    vals.append(run_id)
    with _lock, connect() as c:
        c.execute(f"UPDATE eval_runs SET {', '.join(sets)} WHERE id=?", vals)


def add_eval_result(run_id: int, suite: str, score: float,
                    details: dict | None = None) -> None:
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO eval_results (run_id, suite, score, details_json) VALUES (?,?,?,?)",
            (run_id, suite, round(float(score), 1),
             json.dumps(details or {}, ensure_ascii=False, default=str)),
        )


def get_eval_run(run_id: int) -> Optional[dict]:
    with _lock, connect() as c:
        row = c.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        run = dict(row)
        results = c.execute(
            "SELECT suite, score, details_json FROM eval_results WHERE run_id=? "
            "ORDER BY id", (run_id,)).fetchall()
    run["real_llm"] = bool(run["real_llm"])
    run["use_brain"] = bool(run["use_brain"])
    run["deltas"] = json.loads(run.pop("deltas_json") or "null")
    run["config"] = json.loads(run.pop("config_json") or "{}")
    run["results"] = [{
        "suite": r["suite"], "score": r["score"],
        "details": json.loads(r["details_json"] or "{}"),
    } for r in results]
    return run


def list_eval_runs(limit: int = 50) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT id, ts, label, real_llm, use_brain, status, total, deltas_json "
            "FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["real_llm"] = bool(d["real_llm"])
        d["use_brain"] = bool(d["use_brain"])
        deltas = json.loads(d.pop("deltas_json") or "null") or {}
        d["suites"] = deltas  # {suite: {score, delta}} for the list view
        out.append(d)
    return out


def previous_eval_run(run_id: int) -> Optional[dict]:
    """The most recent completed run with a LOWER id (delta baseline).
    A/B rows (any 'ab*' label — pair marker or arms) never shift it."""
    with _lock, connect() as c:
        row = c.execute(
            "SELECT id FROM eval_runs WHERE id < ? AND status='done' "
            "AND label NOT LIKE 'ab%' ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
    return get_eval_run(row["id"]) if row else None
