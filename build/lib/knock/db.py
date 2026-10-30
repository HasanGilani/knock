"""SQLite storage: connection setup and versioned migrations.

One database holds every registered repository. Every table that stores repository
data carries ``repo_id`` and every query filters on it; that is the isolation
boundary between repositories.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE repositories (
        id               TEXT PRIMARY KEY,
        name             TEXT NOT NULL,
        root_path        TEXT NOT NULL UNIQUE,
        created_at       TEXT NOT NULL,
        last_indexed_at  TEXT,
        indexed_revision TEXT,
        generation       INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE files (
        repo_id           TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
        path              TEXT NOT NULL,
        content_hash      TEXT,
        size              INTEGER NOT NULL,
        mtime_ns          INTEGER NOT NULL,
        language          TEXT,
        kind              TEXT,
        status            TEXT NOT NULL,          -- indexed | skipped | error
        skip_reason       TEXT,
        summary           TEXT,                   -- leading module doc (short), used for directory summaries
        redactions        INTEGER NOT NULL DEFAULT 0,
        extractor_version INTEGER NOT NULL DEFAULT 0,
        indexed_at        TEXT NOT NULL,
        indexed_revision  TEXT,
        PRIMARY KEY (repo_id, path)
    );

    CREATE TABLE symbols (
        repo_id   TEXT NOT NULL,
        path      TEXT NOT NULL,
        name      TEXT NOT NULL,
        kind      TEXT NOT NULL,
        line      INTEGER NOT NULL,
        signature TEXT
    );
    CREATE INDEX symbols_repo_path ON symbols(repo_id, path);
    CREATE VIRTUAL TABLE symbols_fts USING fts5(
        terms, repo_id UNINDEXED, path UNINDEXED, name UNINDEXED, kind UNINDEXED,
        line UNINDEXED, signature UNINDEXED, tokenize = 'porter unicode61'
    );

    CREATE TABLE knowledge (
        id                TEXT PRIMARY KEY,
        repo_id           TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
        category          TEXT NOT NULL,
        title             TEXT NOT NULL,
        content           TEXT NOT NULL,
        rationale         TEXT,
        entities          TEXT NOT NULL DEFAULT '[]',   -- JSON array
        origin            TEXT NOT NULL,                -- extracted | git | agent
        extractor         TEXT,
        verification      TEXT NOT NULL,                -- derived | verified | unverified | historical
        confidence        REAL NOT NULL,
        status            TEXT NOT NULL,                -- active | stale | superseded | invalidated
        status_reason     TEXT,
        fingerprint       TEXT NOT NULL,
        observations      INTEGER NOT NULL DEFAULT 1,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        verified_at       TEXT,
        verified_revision TEXT,
        superseded_by     TEXT
    );
    CREATE INDEX knowledge_repo_status ON knowledge(repo_id, status);
    CREATE INDEX knowledge_repo_fp ON knowledge(repo_id, fingerprint);
    CREATE VIRTUAL TABLE knowledge_fts USING fts5(
        title, content, entities, paths, kid UNINDEXED, repo_id UNINDEXED, tokenize = 'porter unicode61'
    );

    -- Evidence: which source supports a knowledge item, and the file hash it was derived from.
    CREATE TABLE knowledge_sources (
        knowledge_id TEXT NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
        repo_id      TEXT NOT NULL,
        path         TEXT NOT NULL,
        extract_key  TEXT,          -- set for extracted items: stable key within the file
        start_line   INTEGER,
        end_line     INTEGER,
        content_hash TEXT,
        revision     TEXT
    );
    CREATE INDEX ks_repo_path ON knowledge_sources(repo_id, path);
    CREATE INDEX ks_kid ON knowledge_sources(knowledge_id);

    CREATE TABLE knowledge_links (
        src_id   TEXT NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
        dst_id   TEXT NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
        relation TEXT NOT NULL,      -- supersedes | conflicts | related
        created_at TEXT NOT NULL,
        PRIMARY KEY (src_id, dst_id, relation)
    );

    CREATE TABLE commits (
        repo_id      TEXT NOT NULL,
        sha          TEXT NOT NULL,
        committed_at TEXT NOT NULL,
        subject      TEXT NOT NULL,
        files        TEXT NOT NULL,   -- JSON array
        PRIMARY KEY (repo_id, sha)
    );
    CREATE TABLE commit_files (
        repo_id TEXT NOT NULL,
        sha     TEXT NOT NULL,
        path    TEXT NOT NULL
    );
    CREATE INDEX commit_files_path ON commit_files(repo_id, path);

    CREATE TABLE index_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_id       TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
        mode          TEXT NOT NULL,         -- full | incremental
        status        TEXT NOT NULL,         -- running | completed | failed | interrupted
        pid           INTEGER,
        started_at    TEXT NOT NULL,
        heartbeat_at  TEXT NOT NULL,
        finished_at   TEXT,
        revision      TEXT,
        stats         TEXT NOT NULL DEFAULT '{}',
        error         TEXT
    );
    CREATE INDEX index_runs_repo ON index_runs(repo_id, id);

    CREATE TABLE query_cache (
        key         TEXT PRIMARY KEY,
        repo_id     TEXT NOT NULL,
        generation  INTEGER NOT NULL,
        value       TEXT NOT NULL,
        size        INTEGER NOT NULL,
        created_at  TEXT NOT NULL,
        last_hit_at TEXT NOT NULL,
        hits        INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX query_cache_lru ON query_cache(last_hit_at);
    CREATE INDEX query_cache_repo ON query_cache(repo_id, generation);

    CREATE TABLE metrics (
        repo_id    TEXT NOT NULL,      -- '' for global
        name       TEXT NOT NULL,
        count      INTEGER NOT NULL DEFAULT 0,
        total      REAL NOT NULL DEFAULT 0,
        max        REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (repo_id, name)
    );
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than this knock build (v{SCHEMA_VERSION}); upgrade knock"
        )
    for version in range(current, SCHEMA_VERSION):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_sql(MIGRATIONS[version]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version + 1}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements (``executescript`` would commit implicitly)."""
    statements, buf = [], []
    for line in script.splitlines():
        stripped = line.split("--", 1)[0] if "--" in line else line
        buf.append(stripped)
        joined = "\n".join(buf).strip()
        if joined.endswith(";") and sqlite3.complete_statement(joined):
            statements.append(joined)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


class Tx:
    """``with Tx(conn):`` runs the block in an IMMEDIATE transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
