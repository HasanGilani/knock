"""Versioned query cache.

Keys hash the operation, every argument that affects the result, the repository id
and the repository's knowledge *generation*. Any mutation of a repository's
knowledge (index run, recorded/updated knowledge) increments the generation and
drops that repository's entries, so a stale entry can never be served. Entries are
bounded by count and bytes with least-recently-used eviction.

Only the ranked knowledge payload is cached. Freshness and per-source "modified in
working tree" annotations are computed on every call, because they depend on the
working tree rather than on the knowledge store.
"""

from __future__ import annotations

import json
import sqlite3
import time

from .config import Config
from .metrics import Metrics
from .util import sha256_hex, utcnow


class QueryCache:
    def __init__(self, conn: sqlite3.Connection, config: Config, metrics: Metrics):
        self.conn = conn
        self.config = config
        self.metrics = metrics

    @staticmethod
    def key(op: str, repo_id: str, generation: int, params: dict) -> str:
        blob = json.dumps({"op": op, "repo": repo_id, "gen": generation, "params": params}, sort_keys=True, separators=(",", ":"))
        return sha256_hex(blob)

    def get(self, key: str, repo_id: str, generation: int):
        t0 = time.perf_counter()
        row = self.conn.execute(
            "SELECT value FROM query_cache WHERE key=? AND repo_id=? AND generation=?", (key, repo_id, generation)
        ).fetchone()
        if row is None:
            self.metrics.incr(repo_id, "cache_miss")
            self.metrics.observe(repo_id, "cache_lookup_ms", (time.perf_counter() - t0) * 1000)
            return None
        self.conn.execute("UPDATE query_cache SET hits = hits + 1, last_hit_at = ? WHERE key = ?", (utcnow(), key))
        self.metrics.incr(repo_id, "cache_hit")
        self.metrics.observe(repo_id, "cache_lookup_ms", (time.perf_counter() - t0) * 1000)
        return json.loads(row["value"])

    def put(self, key: str, repo_id: str, generation: int, value) -> None:
        blob = json.dumps(value, separators=(",", ":"))
        if len(blob) > self.config.cache_max_bytes // 4:
            return
        now = utcnow()
        self.conn.execute(
            """INSERT OR REPLACE INTO query_cache(key, repo_id, generation, value, size, created_at, last_hit_at, hits)
               VALUES (?,?,?,?,?,?,?,0)""",
            (key, repo_id, generation, blob, len(blob), now, now),
        )
        self._evict()

    def _evict(self) -> None:
        count, size = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM query_cache").fetchone()
        if count <= self.config.cache_max_entries and size <= self.config.cache_max_bytes:
            return
        evicted = 0
        for row in self.conn.execute("SELECT key, size FROM query_cache ORDER BY last_hit_at ASC, created_at ASC").fetchall():
            if count <= self.config.cache_max_entries and size <= self.config.cache_max_bytes:
                break
            self.conn.execute("DELETE FROM query_cache WHERE key=?", (row["key"],))
            count -= 1
            size -= row["size"]
            evicted += 1
        self.metrics.incr("", "cache_evictions", evicted)

    def stats(self) -> dict:
        count, size = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM query_cache").fetchone()
        return {"entries": count, "bytes": size, "max_entries": self.config.cache_max_entries, "max_bytes": self.config.cache_max_bytes}
