"""Local, lightweight counters and timings. Nothing is ever transmitted.

Each metric keeps ``count``, ``total`` and ``max``. Names ending in ``_ms`` are
latencies; names starting with ``est_`` are estimates and are reported as such.
"""

from __future__ import annotations

import sqlite3

from .util import utcnow

GLOBAL = ""


class Metrics:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def observe(self, repo_id: str, name: str, value: float = 1.0, count: int = 1) -> None:
        self.conn.execute(
            """INSERT INTO metrics(repo_id, name, count, total, max, updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(repo_id, name) DO UPDATE SET count = count + excluded.count,
               total = total + excluded.total, max = MAX(max, excluded.max), updated_at = excluded.updated_at""",
            (repo_id, name, count, value, value, utcnow()),
        )

    def incr(self, repo_id: str, name: str, n: int = 1) -> None:
        if n:
            self.observe(repo_id, name, float(n), count=n)

    def snapshot(self, repo_id: str) -> dict:
        rows = self.conn.execute("SELECT * FROM metrics WHERE repo_id = ? ORDER BY name", (repo_id,)).fetchall()
        counters, timings, estimates = {}, {}, {}
        for r in rows:
            name = r["name"]
            if name.endswith("_ms"):
                timings[name] = {"count": r["count"], "avg_ms": round(r["total"] / r["count"], 2) if r["count"] else 0, "max_ms": round(r["max"], 2)}
            elif name.startswith("est_"):
                estimates[name] = {"total": int(r["total"]), "count": r["count"]}
            else:
                counters[name] = int(r["total"])
        hits, misses = counters.get("cache_hit", 0), counters.get("cache_miss", 0)
        out = {
            "measured": {"counters": counters, "latency": timings},
            "estimated": estimates,
            "cache": {"hits": hits, "misses": misses, "hit_rate": round(hits / (hits + misses), 3) if hits + misses else None},
            "notes": [
                "measured.* values are counted directly by knock on this machine.",
                "estimated.* values are approximations (characters / 4 ≈ tokens; 'source_chars_referenced' is the size of "
                "files cited by returned knowledge, i.e. an upper bound on what reading them would have cost, not a saving).",
                "Provider-side prompt caching and actual model usage are not visible to knock; see the benchmark harness.",
            ],
        }
        return out
