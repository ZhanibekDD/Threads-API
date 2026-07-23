import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    threads_post_id TEXT UNIQUE NOT NULL,
    author_id TEXT NOT NULL,
    author_username TEXT,
    text TEXT NOT NULL,
    permalink TEXT,
    language TEXT,
    found_keyword TEXT,
    published_at TEXT,
    found_at TEXT NOT NULL,
    problem_type TEXT,
    lead_score INTEGER,
    relevance TEXT,
    decision TEXT,
    status TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS generated_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES threads_posts(id),
    generated_text TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    approved INTEGER NOT NULL DEFAULT 0,
    published INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    threads_reply_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS authors (
    author_id TEXT PRIMARY KEY,
    username TEXT,
    first_seen TEXT NOT NULL,
    last_contacted TEXT,
    replies_count INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    searches INTEGER NOT NULL DEFAULT 0,
    posts_found INTEGER NOT NULL DEFAULT 0,
    relevant_posts INTEGER NOT NULL DEFAULT 0,
    manual_reviews INTEGER NOT NULL DEFAULT 0,
    replies_published INTEGER NOT NULL DEFAULT 0,
    website_cta_count INTEGER NOT NULL DEFAULT 0,
    whatsapp_cta_count INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deepseek_usage (
    date TEXT PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS own_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    language TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    exported INTEGER NOT NULL DEFAULT 0,
    -- A human must set approved=1 (CLI `approve-content` or the panel) before
    -- the scheduler will ever publish this row — the schedule controls WHEN,
    -- a person still controls WHETHER. See app/content_generator.py.
    approved INTEGER NOT NULL DEFAULT 0,
    published INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    threads_post_id TEXT,
    error TEXT
);

-- Single-account internal tool: one row (id=1) holds the current OAuth token.
-- No multi-user/multi-account support by design.
CREATE TABLE IF NOT EXISTS oauth_tokens (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    access_token TEXT NOT NULL,
    obtained_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    threads_user_id TEXT,
    threads_username TEXT,
    updated_at TEXT NOT NULL
);

-- Short-lived CSRF state values for the OAuth connect/callback flow.
-- Each row is deleted the moment it is checked (one-time use).
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(path: str | None = None) -> sqlite3.Connection:
    db_path = path or config.database_path
    if db_path != ":memory:":
        config.ensure_data_dir()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_OWN_CONTENT_MIGRATIONS = [
    "ALTER TABLE own_content ADD COLUMN approved INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE own_content ADD COLUMN published INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE own_content ADD COLUMN published_at TEXT",
    "ALTER TABLE own_content ADD COLUMN threads_post_id TEXT",
    "ALTER TABLE own_content ADD COLUMN error TEXT",
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    # Databases created before these columns existed need a migration —
    # CREATE TABLE IF NOT EXISTS above is a no-op on an existing table.
    for statement in _OWN_CONTENT_MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise
    conn.commit()


@contextmanager
def session(path: str | None = None):
    conn = get_connection(path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def touch_daily_metric(conn: sqlite3.Connection, date: str, **increments: int) -> None:
    conn.execute(
        "INSERT INTO daily_metrics (date) VALUES (?) ON CONFLICT(date) DO NOTHING", (date,)
    )
    for field, delta in increments.items():
        if field not in (
            "searches", "posts_found", "relevant_posts", "manual_reviews",
            "replies_published", "website_cta_count", "whatsapp_cta_count", "errors",
        ):
            raise ValueError(f"unknown daily_metrics field: {field}")
        conn.execute(
            f"UPDATE daily_metrics SET {field} = {field} + ? WHERE date = ?", (delta, date)
        )


def touch_deepseek_usage(conn: sqlite3.Connection, date: str, prompt_tokens: int,
                          completion_tokens: int) -> None:
    conn.execute(
        "INSERT INTO deepseek_usage (date) VALUES (?) ON CONFLICT(date) DO NOTHING", (date,)
    )
    conn.execute(
        "UPDATE deepseek_usage SET requests = requests + 1, "
        "prompt_tokens = prompt_tokens + ?, completion_tokens = completion_tokens + ? "
        "WHERE date = ?",
        (prompt_tokens, completion_tokens, date),
    )
