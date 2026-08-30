"""SQLite persistence layer — all state lives in data/openstanley.db.

v0.5.0 multi-account: an `accounts` registry scopes every content table.
Rows in scoped tables carry account_id; helpers take an optional `acct`
parameter (defaulting to the ACTIVE account from settings). App-level
tables (settings, chat_messages, agent_log, accounts) are NOT scoped.
"""
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
# RLock: scoped helpers may resolve the ACTIVE account (a settings read)
# while already holding the lock — same thread, reentrant by design
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT DEFAULT '',
    created_at TEXT,
    status TEXT DEFAULT 'active',         -- active | archived
    cookies_json TEXT DEFAULT ''           -- per-account X cookies (never logged)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
    x_id TEXT,
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
    metrics_json TEXT,                    -- LATEST metrics capture (history lives in metric_snapshots)
    UNIQUE (account_id, x_id)
);

-- append-only time series (v0.3.6 analytics ground truth — never rewritten)
CREATE TABLE IF NOT EXISTS metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
    post_x_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    likes INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    impressions INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS identity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
    captured_at TEXT NOT NULL,
    followers INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS voice_profile (
    account_id INTEGER PRIMARY KEY,
    rubric TEXT DEFAULT '',
    examples_json TEXT DEFAULT '[]',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
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
    account_id INTEGER NOT NULL DEFAULT 1,
    idea_id INTEGER,
    kind TEXT DEFAULT 'post',        -- post|reply|quote
    text TEXT NOT NULL,
    thread_json TEXT,                -- JSON list for threads, null for single
    status TEXT DEFAULT 'draft',     -- draft|approved|rejected|published|failed
    temperature TEXT DEFAULT 'safe', -- safe|bold|experimental
    scheduled_at TEXT,
    x_id TEXT,
    meta_json TEXT,
    image TEXT,
    quote_of TEXT,                   -- quoted tweet x_id
    created_at TEXT,
    published_at TEXT,
    FOREIGN KEY (idea_id) REFERENCES ideas(id)
);

CREATE TABLE IF NOT EXISTS engagements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL DEFAULT 1,
    x_id TEXT,
    kind TEXT,                        -- mention|reply-to-me|notification
    author_handle TEXT,
    author_name TEXT,
    text TEXT,
    draft_id INTEGER,
    status TEXT DEFAULT 'new',        -- new|drafted|replied|ignored
    created_at TEXT,
    seen_at TEXT,
    UNIQUE (account_id, x_id)
);

-- mention inbox (v0.3.9): every incoming @-mention, deduped per account;
-- handled=1 exactly when a reply draft exists for it
CREATE TABLE IF NOT EXISTS seen_mentions (
    account_id INTEGER NOT NULL DEFAULT 1,
    x_id TEXT,
    author TEXT,
    text TEXT,
    created_at TEXT,
    first_seen TEXT,
    handled INTEGER DEFAULT 0,
    PRIMARY KEY (account_id, x_id)
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
    account_id INTEGER NOT NULL DEFAULT 1,
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
    account_id INTEGER NOT NULL DEFAULT 1,
    run_id INTEGER,
    suite TEXT,
    score REAL,
    details_json TEXT,
    FOREIGN KEY (run_id) REFERENCES eval_runs(id)
);

-- indexes are created in _migrate() AFTER table rebuilds — a fresh SCHEMA run
-- on a pre-v0.5 DB would otherwise index columns that do not exist yet
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_posts_own ON posts(account_id, is_own, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_handle);
CREATE INDEX IF NOT EXISTS idx_metric_snap_post ON metric_snapshots(account_id, post_x_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_identity_snap_time ON identity_snapshots(account_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(account_id, status);
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(account_id, run_id);
"""

# tables whose rows are scoped by account_id (v0.5.0)
SCOPED_TABLES = ("posts", "drafts", "ideas", "engagements", "seen_mentions",
                 "metric_snapshots", "identity_snapshots", "voice_profile",
                 "eval_runs", "eval_results")


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
    """Idempotent migrations. v0.5.0: account scoping — old rows land in
    account 1 (the bootstrap account), nothing is dropped."""
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

    # ---- v0.5.0 multi-account migration ----
    _migrate_accounts(c)

    # constraint rebuilds: uniqueness moved from x_id alone to (account_id, x_id)
    _rebuild_table(c, "posts", """
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL DEFAULT 1,
            x_id TEXT,
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
            metrics_json TEXT,
            UNIQUE (account_id, x_id))""")
    _rebuild_table(c, "engagements", """
        CREATE TABLE engagements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL DEFAULT 1,
            x_id TEXT,
            kind TEXT,
            author_handle TEXT,
            author_name TEXT,
            text TEXT,
            draft_id INTEGER,
            status TEXT DEFAULT 'new',
            created_at TEXT,
            seen_at TEXT,
            UNIQUE (account_id, x_id))""")
    _rebuild_table(c, "seen_mentions", """
        CREATE TABLE seen_mentions (
            account_id INTEGER NOT NULL DEFAULT 1,
            x_id TEXT,
            author TEXT,
            text TEXT,
            created_at TEXT,
            first_seen TEXT,
            handled INTEGER DEFAULT 0,
            PRIMARY KEY (account_id, x_id))""")
    _rebuild_table(c, "voice_profile", """
        CREATE TABLE voice_profile (
            account_id INTEGER PRIMARY KEY,
            rubric TEXT DEFAULT '',
            examples_json TEXT DEFAULT '[]',
            updated_at TEXT)""")

    # simple column adds for the remaining scoped tables
    for table in ("drafts", "ideas", "metric_snapshots", "identity_snapshots",
                  "eval_runs", "eval_results"):
        tcols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        if "account_id" not in tcols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1")

    # ---- v0.5.x: one timestamp format in posts.created_at ----
    # Legacy imports stored X's raw 'Wed Sep 24 14:10:11 +0000 2025' —
    # lexically greater than every ISO cutoff, so time filters matched
    # everything and ORDER BY put the oldest era first (own_posts(500)
    # filled with pre-pivot posts; the voice profile never saw the newest
    # ones). Convert in place; rows that don't parse are left untouched
    # and re-tried next boot (idempotent: ISO rows never match).
    rows = c.execute(
        "SELECT id, created_at FROM posts "
        "WHERE created_at IS NOT NULL AND created_at != '' "
        "AND substr(created_at, 1, 4) GLOB '[0-9][0-9][0-9][0-9]' = 0"
    ).fetchall()
    for r in rows:
        fixed = _norm_created_at(r["created_at"])
        if fixed != r["created_at"]:
            c.execute("UPDATE posts SET created_at=? WHERE id=?",
                      (fixed, r["id"]))

    # scoped indexes (fresh shapes); old unscoped ones get dropped first —
    # CREATE IF NOT EXISTS would silently keep the wrong definition
    for stale in ("idx_posts_own", "idx_metric_snap_post", "idx_identity_snap_time",
                  "idx_drafts_status", "idx_eval_results_run"):
        c.execute(f"DROP INDEX IF EXISTS {stale}")
    c.executescript(INDEXES)


def _rebuild_table(c: sqlite3.Connection, table: str, create_sql: str) -> None:
    """Rebuild a table whose old shape lacks account_id / has the wrong
    uniqueness constraint. Only fires when the old shape is detected; the
    copy stamps every existing row with account_id=1 (bootstrap account)
    and keeps every column the new shape still has."""
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if "account_id" in cols:
        return  # already migrated (or created fresh by SCHEMA)
    c.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
    c.execute(create_sql)
    new_cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    old_cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table}__old)").fetchall()]
    shared = [col for col in new_cols if col in old_cols and col != "account_id"]
    select_cols = ", ".join(shared)
    c.execute(f"INSERT INTO {table} (account_id, {select_cols}) "
              f"SELECT 1, {select_cols} FROM {table}__old")
    c.execute(f"DROP TABLE {table}__old")


def _migrate_accounts(c: sqlite3.Connection) -> None:
    """Seed the accounts registry — the pre-v0.5 single install becomes
    account 1, keeping the handle X knew it by (when we have one)."""
    row = c.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()
    if row["n"] == 0:
        handle = ""
        try:
            me = json.loads(c.execute(
                "SELECT value FROM settings WHERE key='me'").fetchone()["value"])
            handle = str(me.get("username") or "")
        except (TypeError, ValueError, KeyError, sqlite3.Error):
            pass
        c.execute("INSERT INTO accounts (id, handle, created_at, status, cookies_json) "
                  "VALUES (1, ?, ?, 'active', '')", (handle, _now()))


# ---------- accounts (v0.5.0) ----------

def active_account() -> int:
    """The account all loops/UI operate on right now (settings-backed)."""
    try:
        return max(1, int(get_setting("active_account_id", 1) or 1))
    except (TypeError, ValueError):
        return 1


def set_active_account(account_id: int) -> bool:
    with _lock, connect() as c:
        row = c.execute("SELECT id FROM accounts WHERE id=? AND status='active'",
                        (account_id,)).fetchone()
        if row is None:
            return False
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('active_account_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(int(account_id)),))
    return True


def _acct(acct: Optional[int]) -> int:
    return active_account() if acct is None else int(acct)


def get_account(account_id: int) -> Optional[dict]:
    with _lock, connect() as c:
        row = c.execute("SELECT id, handle, created_at, status, cookies_json "
                        "FROM accounts WHERE id=?", (account_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["cookies_json"] = d.pop("cookies_json") or ""
    d["cookies_set"] = bool(d["cookies_json"])
    return d


def list_accounts() -> list[dict]:
    """Registry view: handle, follower snapshot, own-post count (no secrets)."""
    with _lock, connect() as c:
        rows = c.execute("SELECT id, handle, created_at, status, cookies_json "
                         "FROM accounts ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            cookies = d.pop("cookies_json") or ""
            (followers,) = c.execute(
                "SELECT followers FROM identity_snapshots WHERE account_id=? "
                "ORDER BY captured_at DESC, id DESC LIMIT 1", (d["id"],)).fetchone() or (None,)
            (posts_n,) = c.execute(
                "SELECT COUNT(*) FROM posts WHERE account_id=? AND is_own=1",
                (d["id"],)).fetchone()
            d.update({"cookies_set": bool(cookies),
                      "cookies_masked": mask_cookies(cookies),
                      "followers": followers, "own_posts": posts_n,
                      "active": d["id"] == active_account()})
            out.append(d)
    return out


def create_account(handle: str, cookies_json: str = "") -> int:
    handle = (handle or "").strip().lstrip("@")
    with _lock, connect() as c:
        cur = c.execute(
            "INSERT INTO accounts (handle, created_at, status, cookies_json) "
            "VALUES (?,?, 'active', ?)", (handle, _now(), cookies_json or ""))
        return cur.lastrowid


def set_account_handle(account_id: int, handle: str) -> bool:
    handle = (handle or "").strip().lstrip("@")
    with _lock, connect() as c:
        cur = c.execute("UPDATE accounts SET handle=? WHERE id=?",
                        (handle, account_id))
        return cur.rowcount > 0


def account_cookies(account_id: int) -> str:
    """DB-stored cookies for one account ('' when none). Env .env bootstrap
    fallback for account 1 is resolved by the caller (server/client layer)."""
    with _lock, connect() as c:
        row = c.execute("SELECT cookies_json FROM accounts WHERE id=?",
                        (account_id,)).fetchone()
    return (row["cookies_json"] or "") if row else ""


def set_account_cookies(account_id: int, cookies_json: str) -> bool:
    with _lock, connect() as c:
        cur = c.execute("UPDATE accounts SET cookies_json=? WHERE id=?",
                        (cookies_json or "", account_id))
        return cur.rowcount > 0


def mask_cookies(cookies_json: str) -> Optional[str]:
    """Masked hint for GET views — never the values (scrubbed like the TG token)."""
    if not cookies_json:
        return None
    try:
        data = json.loads(cookies_json)
        token = str(data.get("auth_token") or "")
    except (ValueError, TypeError):
        token = ""
    if len(token) > 4:
        return f"••••{token[-4:]}"
    return "(set)"


def dump_account(account_id: int) -> dict:
    """All scoped rows for one account (for archiving before deletion)."""
    with _lock, connect() as c:
        out: dict[str, list] = {}
        for table in SCOPED_TABLES:
            rows = c.execute(
                f"SELECT * FROM {table} WHERE account_id=?", (account_id,)).fetchall()
            out[table] = [dict(r) for r in rows]
    return out


def delete_account_rows(account_id: int, keep_account: bool = False) -> None:
    """Remove one account's scoped rows (and the account row itself unless
    keep_account — used when resetting a broken connect)."""
    with _lock, connect() as c:
        for table in SCOPED_TABLES:
            c.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
        if not keep_account:
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))


# ---------- per-account identity ("me") ----------

def set_me(me: dict, acct: Optional[int] = None) -> None:
    """Account 1 IS the legacy pre-v0.5 account — its identity lives in the
    original 'me' settings key; other accounts get 'me:<id>'."""
    a = _acct(acct)
    set_setting("me" if a == 1 else f"me:{a}", me)


def get_me(acct: Optional[int] = None) -> dict:
    a = _acct(acct)
    return get_setting("me" if a == 1 else f"me:{a}") or {}


def _acct_setting(key: str, acct: Optional[int]) -> str:
    """Settings key for per-account data living in the app-level settings
    table (style_profile, strategy, …). Account 1 keeps the legacy key."""
    a = _acct(acct)
    return key if a == 1 else f"{key}:{a}"


def get_acct_setting(key: str, default: Any = None,
                     acct: Optional[int] = None) -> Any:
    return get_setting(_acct_setting(key, acct), default)


def set_acct_setting(key: str, value: Any, acct: Optional[int] = None) -> None:
    set_setting(_acct_setting(key, acct), value)


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


# ---------- chat (OpenStanley agent; app-level, chat_id-scoped) ----------

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


def session_watermark() -> str:
    """ISO timestamp of the last 05:00 session reset ('' = carry full
    history). Both frontends start a FRESH session each morning (like
    Hermes): the watermark bounds what _history_turn can see."""
    return str(get_setting("chat_session_reset_at") or "")


def chat_history(limit: int = 40) -> list[dict]:
    """Dashboard chat history — Telegram turns (chat_id set) are excluded so
    the two frontends never mix histories. Bounded by the session
    watermark: yesterday's conversation stays in the DB but leaves the
    context each morning."""
    wm = session_watermark()
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT id, role, content, ts, meta_json FROM chat_messages "
            "WHERE chat_id IS NULL"
            + (" AND ts > ?" if wm else "") +
            " ORDER BY id DESC LIMIT ?",
            ((wm, limit) if wm else (limit,)),
        ).fetchall()
    out = []
    for r in reversed(rows):
        out.append({
            "id": r["id"], "role": r["role"], "content": r["content"],
            "ts": r["ts"], "meta": json.loads(r["meta_json"] or "{}"),
        })
    return out


def chat_history_for_chat(chat_id: int, limit: int = 40) -> list[dict]:
    """One Telegram chat's persisted turns, oldest → newest, bounded by the
    session watermark (fresh session each morning)."""
    wm = session_watermark()
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT id, role, content, ts, meta_json FROM chat_messages "
            "WHERE chat_id=?"
            + (" AND ts > ?" if wm else "") +
            " ORDER BY id DESC LIMIT ?",
            ((chat_id, wm, limit) if wm else (chat_id, limit)),
        ).fetchall()
    out = []
    for r in reversed(rows):
        out.append({
            "id": r["id"], "role": r["role"], "content": r["content"],
            "ts": r["ts"], "meta": json.loads(r["meta_json"] or "{}"),
        })
    return out


# ---------- posts ----------

def _norm_created_at(v) -> str:
    """Normalize ANY X/API timestamp shape to local-naive ISO.

    twikit emits 'Fri Apr 03 16:25:49 +0000 2026'; some paths lose the
    year, leaving 25 chars no parser accepts — the engage gate then
    rejected every target as 'age unknown'. Worse, raw X strings break
    EVERY lexical SQL comparison ('Wed...' > '2026-...' because 'W' >
    '2'), so week/month/all cutoffs all matched every post: live
    2026-08-31 'best post this week' returned the account's LIFETIME
    totals (820 posts, 64.3M impressions) for every timeframe — owner:
    'you are lying'. One canonical ISO form, written at this boundary
    and migrated into old rows, so SQL means what it says again."""
    s = str(v or "")
    if len(s) == 25 and s[3] == " " and "+" in s[19:25]:
        s = f"{s} {datetime.now().year}"
    if s[:4].isdigit():
        return s  # already ISO-shaped
    # X format — parse, shift to local, drop tz: the app speaks local-naive
    # ISO everywhere (scheduled_at, cutoffs, watermarks)
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S %Y"):
        try:
            ts = datetime.strptime(s, fmt)
            if ts.tzinfo is not None:
                ts = ts.astimezone().replace(tzinfo=None)
            return ts.isoformat(timespec="seconds")
        except ValueError:
            continue
    return s  # unparseable — leave for the dual-format readers


def _post_exists(x_id, acct: Optional[int] = None) -> bool:
    with connect() as c:
        row = c.execute("SELECT 1 FROM posts WHERE account_id=? AND x_id=?",
                        (_acct(acct), x_id)).fetchone()
    return row is not None


def upsert_post(p: dict, acct: Optional[int] = None) -> None:
    # Algorithm-weighted engagement (X 2025-26 weights: reply ≈27-75x like, RT ≈2x like).
    # Pragmatic skew 1:3:8 (likes:reposts:replies) — full 1:2:54 ratio overweights
    # single-viral-reply posts for ranking a personal corpus.
    eng = float(p.get("likes", 0)) + 3 * float(p.get("reposts", 0)) + 8 * float(p.get("replies", 0))
    impressions = float(p.get("impressions") or 0)
    rate = (eng / impressions) if impressions > 0 else 0.0
    with _lock, connect() as c:
        c.execute(
            """INSERT INTO posts (account_id, x_id, author_handle, is_own, created_at, text,
               impressions, likes, reposts, replies, bookmarks, engagement, topics, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id, x_id) DO UPDATE SET
                 impressions=excluded.impressions, likes=excluded.likes,
                 reposts=excluded.reposts, replies=excluded.replies,
                 bookmarks=excluded.bookmarks, engagement=excluded.engagement""",
            (
                _acct(acct), p.get("x_id"), p.get("author_handle", ""), int(p.get("is_own", 0)),
                _norm_created_at(p.get("created_at")), p.get("text", ""),
                int(p.get("impressions", 0)), int(p.get("likes", 0)),
                int(p.get("reposts", 0)), int(p.get("replies", 0)),
                int(p.get("bookmarks", 0)), round(rate, 5),
                json.dumps(p.get("topics", [])), json.dumps(p.get("raw", {})),
            ),
        )


def own_posts(limit: int = 500, acct: Optional[int] = None) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE account_id=? AND is_own=1 "
            "ORDER BY created_at DESC LIMIT ?", (_acct(acct), limit)
        ).fetchall()
    return [dict(r) for r in rows]


def niche_posts(limit: int = 400, acct: Optional[int] = None) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE account_id=? AND is_own=0 AND engagement > 0 "
            "ORDER BY engagement DESC LIMIT ?", (_acct(acct), limit)
        ).fetchall()
    return [dict(r) for r in rows]


def posts_needing_metrics(hours: int = 72, limit: int = 100,
                          acct: Optional[int] = None) -> list[dict]:
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE account_id=? AND is_own=1 AND created_at > ? "
            "AND x_id IS NOT NULL ORDER BY created_at DESC LIMIT ?",
            (_acct(acct), cutoff, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- voice ----------

def save_voice(rubric: str, examples: list[dict], acct: Optional[int] = None) -> None:
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO voice_profile (account_id, rubric, examples_json, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
            "rubric=excluded.rubric, examples_json=excluded.examples_json, "
            "updated_at=excluded.updated_at",
            (_acct(acct), rubric, json.dumps(examples, ensure_ascii=False), _now()),
        )


def load_voice(acct: Optional[int] = None) -> Optional[dict]:
    with _lock, connect() as c:
        row = c.execute("SELECT * FROM voice_profile WHERE account_id=?",
                        (_acct(acct),)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["examples"] = json.loads(d.pop("examples_json") or "[]")
    return d


# ---------- ideas ----------

def add_idea(title: str, angle: str, fmt: str, source: str, score: float = 0.0,
             acct: Optional[int] = None) -> int:
    with _lock, connect() as c:
        cur = c.execute(
            "INSERT INTO ideas (account_id, title, angle, format, source, score, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (_acct(acct), title, angle, fmt, source, score, _now()),
        )
        return cur.lastrowid


def fresh_ideas(limit: int = 20, acct: Optional[int] = None) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM ideas WHERE account_id=? AND status='new' "
            "ORDER BY score DESC, created_at DESC LIMIT ?",
            (_acct(acct), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def idea_count(acct: Optional[int] = None) -> int:
    with _lock, connect() as c:
        (n,) = c.execute("SELECT COUNT(*) FROM ideas WHERE account_id=? AND status='new'",
                         (_acct(acct),)).fetchone()
    return n


def mark_idea(idea_id: int, status: str, acct: Optional[int] = None) -> None:
    with _lock, connect() as c:
        c.execute(
            "UPDATE ideas SET status=?, used_at=CASE WHEN ?='used' THEN ? ELSE used_at END "
            "WHERE id=? AND account_id=?",
            (status, status, _now(), idea_id, _acct(acct)),
        )


# ---------- drafts ----------

def add_draft(text: str, idea_id: Optional[int] = None, kind: str = "post",
              thread: Optional[list[str]] = None, temperature: str = "safe",
              meta: Optional[dict] = None, image: Optional[str] = None,
              quote_of: Optional[str] = None,
              scheduled_at: Optional[str] = None,
              status: str = "draft", acct: Optional[int] = None) -> int:
    from .text import scrub_ai_punctuation
    text = scrub_ai_punctuation(text)
    if thread:
        thread = [scrub_ai_punctuation(t) for t in thread]
    with _lock, connect() as c:
        cur = c.execute(
            "INSERT INTO drafts (account_id, idea_id, kind, text, thread_json, status, "
            "temperature, meta_json, image, quote_of, scheduled_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_acct(acct), idea_id, kind, text, json.dumps(thread) if thread else None,
             status, temperature, json.dumps(meta or {}, ensure_ascii=False),
             image, quote_of, scheduled_at, _now()),
        )
        return cur.lastrowid


def delete_scheduled_drafts(acct: Optional[int] = None) -> int:
    """Delete every scheduled-but-unpublished draft (the Calendar's placed
    cards). Published history and rejected drafts are never touched."""
    with _lock, connect() as c:
        cur = c.execute(
            "DELETE FROM drafts WHERE account_id=? AND status IN ('approved','draft') "
            "AND scheduled_at IS NOT NULL", (_acct(acct),))
        return cur.rowcount


def delete_queued_drafts(acct: Optional[int] = None) -> int:
    """Delete every unscheduled draft (the Calendar's Queue rail)."""
    with _lock, connect() as c:
        cur = c.execute(
            "DELETE FROM drafts WHERE account_id=? AND status IN ('approved','draft') "
            "AND scheduled_at IS NULL", (_acct(acct),))
        return cur.rowcount


def update_draft(draft_id: int, acct: Optional[int] = None, **fields: Any) -> None:
    allowed = {"text", "thread_json", "status", "scheduled_at", "x_id", "published_at",
               "meta_json", "image", "quote_of", "kind", "temperature"}
    sets, vals = [], []
    from .text import scrub_ai_punctuation
    if "text" in fields and fields["text"]:
        fields["text"] = scrub_ai_punctuation(fields["text"])
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
    vals.append(_acct(acct))
    with _lock, connect() as c:
        c.execute(f"UPDATE drafts SET {', '.join(sets)} WHERE id=? AND account_id=?", vals)


def drafts_by_status(status: str, limit: int = 100,
                     acct: Optional[int] = None) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT * FROM drafts WHERE account_id=? AND status=? "
            "ORDER BY created_at DESC LIMIT ?", (_acct(acct), status, limit)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["thread"] = json.loads(d.pop("thread_json") or "null")
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        out.append(d)
    return out


def next_scheduled(acct: Optional[int] = None) -> Optional[dict]:
    with _lock, connect() as c:
        row = c.execute(
            "SELECT * FROM drafts WHERE account_id=? AND status='approved' "
            "AND scheduled_at IS NOT NULL AND scheduled_at <= ? "
            "ORDER BY scheduled_at LIMIT 1",
            (_acct(acct), _now()),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["thread"] = json.loads(d.pop("thread_json") or "null")
    d["meta"] = json.loads(d.pop("meta_json") or "{}")
    return d


def get_draft(draft_id: int, acct: Optional[int] = None) -> Optional[dict]:
    """Fetch one draft of any status (active-account scoped), with parsed
    thread/meta."""
    with _lock, connect() as c:
        row = c.execute("SELECT * FROM drafts WHERE id=? AND account_id=?",
                        (draft_id, _acct(acct))).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["thread"] = json.loads(d.pop("thread_json") or "null")
    d["meta"] = json.loads(d.pop("meta_json") or "{}")
    return d


def dashboard_stats(acct: Optional[int] = None) -> dict:
    a = _acct(acct)
    with _lock, connect() as c:
        counts = {}
        for status in ("draft", "approved", "published", "rejected", "failed"):
            (counts[status],) = c.execute(
                "SELECT COUNT(*) FROM drafts WHERE account_id=? AND status=? AND kind='post'",
                (a, status)).fetchone()
        (own_count,) = c.execute(
            "SELECT COUNT(*) FROM posts WHERE account_id=? AND is_own=1", (a,)).fetchone()
        (niche_count,) = c.execute(
            "SELECT COUNT(*) FROM posts WHERE account_id=? AND is_own=0", (a,)).fetchone()
        (eng_new,) = c.execute(
            "SELECT COUNT(*) FROM engagements WHERE account_id=? AND status='new'",
            (a,)).fetchone()
    return {
        "drafts": counts, "own_posts": own_count, "niche_posts": niche_count,
        "new_engagements": eng_new, "ideas_bank": idea_count(a),
    }


# ---------- harness (eval runs) ----------

def add_eval_run(label: str = "manual", real_llm: bool = False,
                 use_brain: bool = True, config: dict | None = None,
                 acct: Optional[int] = None) -> int:
    with _lock, connect() as c:
        cur = c.execute(
            "INSERT INTO eval_runs (account_id, ts, label, real_llm, use_brain, status, config_json) "
            "VALUES (?,?,?,?,?, 'running', ?)",
            (_acct(acct), _now(), label, int(real_llm), int(use_brain),
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
                    details: dict | None = None, acct: Optional[int] = None) -> None:
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO eval_results (account_id, run_id, suite, score, details_json) "
            "VALUES (?,?,?,?,?)",
            (_acct(acct), run_id, suite, round(float(score), 1),
             json.dumps(details or {}, ensure_ascii=False, default=str)),
        )


def get_eval_run(run_id: int, acct: Optional[int] = None) -> Optional[dict]:
    with _lock, connect() as c:
        row = c.execute("SELECT * FROM eval_runs WHERE id=? AND account_id=?",
                        (run_id, _acct(acct))).fetchone()
        if row is None:
            return None
        run = dict(row)
        results = c.execute(
            "SELECT suite, score, details_json FROM eval_results "
            "WHERE run_id=? AND account_id=? ORDER BY id",
            (run_id, _acct(acct))).fetchall()
    run["real_llm"] = bool(run["real_llm"])
    run["use_brain"] = bool(run["use_brain"])
    run["deltas"] = json.loads(run.pop("deltas_json") or "null")
    run["config"] = json.loads(run.pop("config_json") or "{}")
    run["results"] = [{
        "suite": r["suite"], "score": r["score"],
        "details": json.loads(r["details_json"] or "{}"),
    } for r in results]
    return run


def list_eval_runs(limit: int = 50, acct: Optional[int] = None) -> list[dict]:
    with _lock, connect() as c:
        rows = c.execute(
            "SELECT id, ts, label, real_llm, use_brain, status, total, deltas_json "
            "FROM eval_runs WHERE account_id=? ORDER BY id DESC LIMIT ?",
            (_acct(acct), limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["real_llm"] = bool(d["real_llm"])
        d["use_brain"] = bool(d["use_brain"])
        deltas = json.loads(d.pop("deltas_json") or "null") or {}
        d["suites"] = deltas  # {suite: {score, delta}} for the list view
        out.append(d)
    return out


def previous_eval_run(run_id: int, acct: Optional[int] = None) -> Optional[dict]:
    """The most recent completed run with a LOWER id (delta baseline).
    A/B rows (any 'ab*' label — pair marker or arms) never shift it."""
    with _lock, connect() as c:
        row = c.execute(
            "SELECT id FROM eval_runs WHERE id < ? AND account_id=? AND status='done' "
            "AND label NOT LIKE 'ab%' ORDER BY id DESC LIMIT 1",
            (run_id, _acct(acct))).fetchone()
    return get_eval_run(row["id"], acct=acct) if row else None
