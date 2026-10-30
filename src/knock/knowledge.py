"""The knowledge store: items, evidence, lifecycle, deduplication.

Lifecycle
---------
``active``       current and supported by its sources.
``stale``        a source changed or disappeared after the item was recorded; it needs
                 re-verification and is returned with a warning.
``superseded``   replaced by a newer item (``superseded_by``); kept for history.
``invalidated``  no longer true (source content no longer produces it, or explicitly
                 invalidated); kept for history, excluded from default retrieval.

Verification
------------
``derived``      deterministically extracted from a source file at a known hash.
``verified``     confirmed by an agent/human against sources at a recorded revision.
``unverified``   asserted by an agent; not yet confirmed.
``historical``   a statement about the past (e.g. a commit); true as history, not as current state.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .extractors import CATEGORIES, Candidate
from .util import clean_text, jaccard, new_id, normalize_text, sha256_hex, terms, utcnow

ACTIVE_STATUSES = ("active", "stale")
ALL_STATUSES = ("active", "stale", "superseded", "invalidated")

NEAR_DUPLICATE = 0.8
CONFLICT_FLOOR = 0.45


def fingerprint(category: str, content: str) -> str:
    return sha256_hex(f"{category}\n{normalize_text(content)}")[:32]


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deduplicated: int = 0
    invalidated: int = 0
    stale: int = 0

    def add(self, other: "SyncStats") -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(other, k))


@dataclass
class SourceRef:
    path: str
    start_line: int | None = None
    end_line: int | None = None
    content_hash: str | None = None
    revision: str | None = None
    extract_key: str | None = None


@dataclass
class RecordResult:
    id: str
    outcome: str                                   # created | deduplicated
    duplicate_of: str | None = None
    similarity: float | None = None
    possible_conflicts: list[dict] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)


class KnowledgeStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------ FTS
    def _fts_refresh(self, kid: str) -> None:
        self.conn.execute("DELETE FROM knowledge_fts WHERE kid = ?", (kid,))
        row = self.conn.execute("SELECT * FROM knowledge WHERE id = ?", (kid,)).fetchone()
        if row is None:
            return
        paths = [r["path"] for r in self.conn.execute("SELECT DISTINCT path FROM knowledge_sources WHERE knowledge_id = ?", (kid,))]
        ents = json.loads(row["entities"])
        self.conn.execute(
            "INSERT INTO knowledge_fts(title, content, entities, paths, kid, repo_id) VALUES (?,?,?,?,?,?)",
            (
                " ".join([row["title"], *terms(row["title"])]),
                " ".join([row["content"], row["rationale"] or "", *terms(row["content"])]),
                " ".join([*ents, *terms(" ".join(ents))]),
                " ".join([*paths, *terms(" ".join(paths))]),
                kid,
                row["repo_id"],
            ),
        )

    # ------------------------------------------------- extracted (per scope)
    def sync_extracted(
        self,
        repo_id: str,
        path: str,
        candidates: list[Candidate],
        content_hash: str | None,
        revision: str | None,
        origin: str = "extracted",
        extractor: str = "deterministic",
        verification: str = "derived",
        extra_paths: dict[str, list[str]] | None = None,
    ) -> SyncStats:
        """Make the extracted knowledge supported by ``path`` match ``candidates`` exactly.

        Must be called inside a transaction. Items are matched by fingerprint (exact
        duplicates anywhere in the repository share one item) or, when the content
        of a section changed, by the stable ``extract_key`` so the item id survives.
        """
        stats = SyncStats()
        now = utcnow()
        existing = {
            r["extract_key"]: r
            for r in self.conn.execute(
                """SELECT ks.rowid AS sid, ks.knowledge_id, ks.extract_key, k.fingerprint, k.status
                   FROM knowledge_sources ks JOIN knowledge k ON k.id = ks.knowledge_id
                   WHERE ks.repo_id = ? AND ks.path = ? AND ks.extract_key IS NOT NULL AND k.origin = ?""",
                (repo_id, path, origin),
            )
        }
        produced: set[str] = set()
        touched: set[str] = set()
        for c in candidates:
            if c.key in produced:
                continue
            produced.add(c.key)
            fp = fingerprint(c.category, c.content)
            prev = existing.get(c.key)
            if prev is not None and prev["fingerprint"] == fp:
                kid = prev["knowledge_id"]
                self.conn.execute(
                    "UPDATE knowledge_sources SET content_hash=?, revision=?, start_line=?, end_line=? WHERE rowid=?",
                    (content_hash, revision, c.start_line, c.end_line, prev["sid"]),
                )
                # An explicit invalidation stays until the source content itself changes.
                self.conn.execute(
                    "UPDATE knowledge SET verified_at=?, verified_revision=?, title=? WHERE id=?",
                    (now, revision, clean_text(c.title, 200), kid),
                )
                stats.unchanged += 1
                touched.add(kid)
                continue
            dup = self.conn.execute(
                "SELECT id FROM knowledge WHERE repo_id=? AND fingerprint=? AND origin=? AND status IN ('active','stale','superseded') LIMIT 1",
                (repo_id, fp, origin),
            ).fetchone()
            if prev is not None:
                # The section changed. Detach it from the old item first.
                self.conn.execute("DELETE FROM knowledge_sources WHERE rowid = ?", (prev["sid"],))
            if dup is not None:
                kid = dup["id"]
                self._add_source(kid, repo_id, path, c, content_hash, revision, c.key)
                self.conn.execute("UPDATE knowledge SET verified_at=?, verified_revision=?, observations=observations+1 WHERE id=?", (now, revision, kid))
                # Same file re-keyed (e.g. a heading was renamed): keep the title current.
                self.conn.execute(
                    """UPDATE knowledge SET title=? WHERE id=? AND NOT EXISTS (SELECT 1 FROM knowledge_sources
                       WHERE knowledge_id=? AND path != ? AND extract_key IS NOT NULL)""",
                    (clean_text(c.title, 200), kid, kid, path),
                )
                stats.deduplicated += 1
            elif prev is not None and not self._has_sources(prev["knowledge_id"]):
                kid = prev["knowledge_id"]
                self._update_item(kid, c, fp, revision, now)
                self._add_source(kid, repo_id, path, c, content_hash, revision, c.key)
                stats.updated += 1
            else:
                kid = self._insert_item(repo_id, c, fp, origin, extractor, verification, revision, now)
                self._add_source(kid, repo_id, path, c, content_hash, revision, c.key)
                stats.created += 1
            extras = (extra_paths or {}).get(c.key)
            if extras is not None:
                self.conn.execute("DELETE FROM knowledge_sources WHERE knowledge_id=? AND extract_key IS NULL", (kid,))
                blank = Candidate(key="", category=c.category, title="", content="")
                for extra in dict.fromkeys(extras):
                    if extra != path:
                        self._add_source(kid, repo_id, extra, blank, None, revision, None)
            touched.add(kid)
            if prev is not None and prev["knowledge_id"] != kid:
                stats.invalidated += self._retire_if_orphan(prev["knowledge_id"], f"source {path} no longer contains this at {_short(revision)}")
            self._fts_refresh(kid)
        for key, prev in existing.items():
            if key in produced:
                continue
            self.conn.execute("DELETE FROM knowledge_sources WHERE rowid = ?", (prev["sid"],))
            stats.invalidated += self._retire_if_orphan(prev["knowledge_id"], f"no longer present in {path} at {_short(revision)}")
        return stats

    def _has_sources(self, kid: str) -> bool:
        return self.conn.execute("SELECT 1 FROM knowledge_sources WHERE knowledge_id=? AND extract_key IS NOT NULL LIMIT 1", (kid,)).fetchone() is not None

    def _retire_if_orphan(self, kid: str, reason: str) -> int:
        if self._has_sources(kid):
            return 0
        cur = self.conn.execute(
            "UPDATE knowledge SET status='invalidated', status_reason=?, updated_at=? WHERE id=? AND status != 'invalidated'",
            (reason, utcnow(), kid),
        )
        # Non-evidence links (e.g. extra paths) no longer matter once retired.
        self.conn.execute("DELETE FROM knowledge_sources WHERE knowledge_id=? AND extract_key IS NULL", (kid,))
        self._fts_refresh(kid)
        return cur.rowcount

    def _insert_item(self, repo_id: str, c: Candidate, fp: str, origin: str, extractor: str, verification: str, revision: str | None, now: str) -> str:
        kid = new_id("k")
        self.conn.execute(
            """INSERT INTO knowledge(id, repo_id, category, title, content, rationale, entities, origin, extractor,
               verification, confidence, status, status_reason, fingerprint, created_at, updated_at, verified_at, verified_revision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kid, repo_id, c.category, clean_text(c.title, 200), clean_text(c.content, 4000), clean_text(c.rationale, 2000) if c.rationale else None,
             json.dumps(sorted(set(c.entities))[:50]), origin, extractor, verification, c.confidence, c.status, c.status_reason,
             fp, now, now, now, revision),
        )
        return kid

    def _update_item(self, kid: str, c: Candidate, fp: str, revision: str | None, now: str) -> None:
        self.conn.execute(
            """UPDATE knowledge SET category=?, title=?, content=?, rationale=?, entities=?, confidence=?, status=?,
               status_reason=?, fingerprint=?, updated_at=?, verified_at=?, verified_revision=? WHERE id=?""",
            (c.category, clean_text(c.title, 200), clean_text(c.content, 4000), clean_text(c.rationale, 2000) if c.rationale else None,
             json.dumps(sorted(set(c.entities))[:50]), c.confidence, c.status, c.status_reason, fp, now, now, revision, kid),
        )

    def _add_source(self, kid: str, repo_id: str, path: str, c: Candidate, content_hash: str | None, revision: str | None, key: str | None) -> None:
        self.conn.execute(
            "INSERT INTO knowledge_sources(knowledge_id, repo_id, path, extract_key, start_line, end_line, content_hash, revision) VALUES (?,?,?,?,?,?,?,?)",
            (kid, repo_id, path, key, c.start_line, c.end_line, content_hash, revision),
        )

    # -------------------------------------------- non-extracted lifecycle
    def mark_path_changed(self, repo_id: str, path: str, new_hash: str | None, revision: str | None) -> int:
        """Agent-recorded items that cite ``path`` at a different hash become stale."""
        reason = (
            f"source {path} was deleted (detected at {_short(revision)})" if new_hash is None
            else f"source {path} changed since this was recorded (detected at {_short(revision)})"
        )
        rows = self.conn.execute(
            """SELECT DISTINCT k.id FROM knowledge k JOIN knowledge_sources ks ON ks.knowledge_id = k.id
               WHERE ks.repo_id=? AND ks.path=? AND ks.extract_key IS NULL AND k.origin='agent'
               AND k.status='active' AND (ks.content_hash IS NULL OR ks.content_hash != ? OR ? IS NULL)""",
            (repo_id, path, new_hash or "", new_hash),
        ).fetchall()
        for r in rows:
            self.conn.execute("UPDATE knowledge SET status='stale', status_reason=?, updated_at=? WHERE id=?", (reason, utcnow(), r["id"]))
        return len(rows)

    def rename_path(self, repo_id: str, old: str, new: str) -> int:
        """A pure rename keeps agent knowledge valid; only its evidence path moves."""
        cur = self.conn.execute(
            "UPDATE knowledge_sources SET path=? WHERE repo_id=? AND path=? AND extract_key IS NULL",
            (new, repo_id, old),
        )
        for r in self.conn.execute("SELECT DISTINCT knowledge_id FROM knowledge_sources WHERE repo_id=? AND path=?", (repo_id, new)).fetchall():
            self._fts_refresh(r["knowledge_id"])
        return cur.rowcount

    # ------------------------------------------------------ agent records
    def record(
        self,
        repo_id: str,
        category: str,
        content: str,
        title: str | None,
        rationale: str | None,
        entities: list[str],
        sources: list[SourceRef],
        revision: str | None,
        confidence: float,
        supersedes: list[str],
    ) -> RecordResult:
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
        content = clean_text(content)
        if len(content) < 10:
            raise ValueError("content is too short to be useful knowledge")
        title = clean_text(title or content.split(". ")[0], 160)
        fp = fingerprint(category, content)
        now = utcnow()
        for sid in supersedes:
            if not self.conn.execute("SELECT 1 FROM knowledge WHERE id=? AND repo_id=?", (sid, repo_id)).fetchone():
                raise ValueError(f"cannot supersede unknown knowledge id {sid!r} in this repository")

        exact = self.conn.execute(
            "SELECT id FROM knowledge WHERE repo_id=? AND fingerprint=? AND status IN ('active','stale') LIMIT 1",
            (repo_id, fp),
        ).fetchone()
        if exact is not None and exact["id"] not in supersedes:
            self._observe(exact["id"], repo_id, sources, revision, now)
            return RecordResult(id=exact["id"], outcome="deduplicated", duplicate_of=exact["id"], similarity=1.0)

        mine = set(terms(content))
        best, conflicts = None, []
        for row in self._similar(repo_id, category, content, exclude=supersedes):
            sim = jaccard(mine, set(terms(row["content"])))
            if sim >= NEAR_DUPLICATE and (best is None or sim > best[1]):
                best = (row["id"], sim)
            elif sim >= CONFLICT_FLOOR and _conflicting(content, row["content"]):
                conflicts.append({"id": row["id"], "content": clean_text(row["content"], 240), "similarity": round(sim, 2)})
        if best is not None:
            self._observe(best[0], repo_id, sources, revision, now)
            return RecordResult(id=best[0], outcome="deduplicated", duplicate_of=best[0], similarity=round(best[1], 2))

        c = Candidate(key="", category=category, title=title, content=content, rationale=rationale,
                      entities=entities, confidence=max(0.0, min(confidence, 1.0)))
        kid = self._insert_item(repo_id, c, fp, "agent", "agent", "unverified", revision, now)
        for s in sources:
            self.conn.execute(
                "INSERT INTO knowledge_sources(knowledge_id, repo_id, path, extract_key, start_line, end_line, content_hash, revision) VALUES (?,?,?,?,?,?,?,?)",
                (kid, repo_id, s.path, None, s.start_line, s.end_line, s.content_hash, revision),
            )
        for other in conflicts:
            self._link(kid, other["id"], "conflicts", now)
        for sid in supersedes:
            self.conn.execute(
                "UPDATE knowledge SET status='superseded', superseded_by=?, status_reason=?, updated_at=? WHERE id=? AND repo_id=?",
                (kid, f"superseded by {kid}", now, sid, repo_id),
            )
            self._link(kid, sid, "supersedes", now)
            self._fts_refresh(sid)
        self._fts_refresh(kid)
        return RecordResult(id=kid, outcome="created", possible_conflicts=conflicts, superseded=list(supersedes))

    def _observe(self, kid: str, repo_id: str, sources: list[SourceRef], revision: str | None, now: str) -> None:
        self.conn.execute("UPDATE knowledge SET observations=observations+1, updated_at=? WHERE id=?", (now, kid))
        have = {(r["path"], r["start_line"], r["end_line"]) for r in self.conn.execute("SELECT path, start_line, end_line FROM knowledge_sources WHERE knowledge_id=?", (kid,))}
        for s in sources:
            if (s.path, s.start_line, s.end_line) not in have:
                self.conn.execute(
                    "INSERT INTO knowledge_sources(knowledge_id, repo_id, path, extract_key, start_line, end_line, content_hash, revision) VALUES (?,?,?,?,?,?,?,?)",
                    (kid, repo_id, s.path, None, s.start_line, s.end_line, s.content_hash, revision),
                )
        self._fts_refresh(kid)

    def _similar(self, repo_id: str, category: str, content: str, exclude: list[str]) -> list[sqlite3.Row]:
        query = fts_query(terms(content)[:24])
        if not query:
            return []
        rows = self.conn.execute(
            """SELECT k.id, k.content FROM knowledge_fts f JOIN knowledge k ON k.id = f.kid
               WHERE knowledge_fts MATCH ? AND f.repo_id = ? AND k.repo_id = ? AND k.category = ?
               AND k.status IN ('active','stale') ORDER BY bm25(knowledge_fts) LIMIT 25""",
            (query, repo_id, repo_id, category),
        ).fetchall()
        return [r for r in rows if r["id"] not in exclude]

    def _link(self, src: str, dst: str, relation: str, now: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO knowledge_links(src_id, dst_id, relation, created_at) VALUES (?,?,?,?)", (src, dst, relation, now))

    def set_status(self, repo_id: str, kid: str, action: str, reason: str | None, current_hashes: dict[str, str | None], revision: str | None) -> dict:
        row = self.conn.execute("SELECT * FROM knowledge WHERE id=? AND repo_id=?", (kid, repo_id)).fetchone()
        if row is None:
            raise KeyError(kid)
        now = utcnow()
        if action == "verify":
            if row["origin"] != "agent":
                raise ValueError("only agent-recorded knowledge can be verified; extracted knowledge is re-derived from its sources on refresh")
            missing = [p for p, h in current_hashes.items() if h is None]
            if missing:
                raise ValueError(f"cannot verify: cited source(s) no longer indexed: {', '.join(missing)}")
            for path, h in current_hashes.items():
                self.conn.execute("UPDATE knowledge_sources SET content_hash=?, revision=? WHERE knowledge_id=? AND path=?", (h, revision, kid, path))
            self.conn.execute(
                "UPDATE knowledge SET status='active', verification='verified', status_reason=?, verified_at=?, verified_revision=?, updated_at=? WHERE id=?",
                (clean_text(reason, 300) if reason else None, now, revision, now, kid),
            )
        elif action == "invalidate":
            self.conn.execute(
                "UPDATE knowledge SET status='invalidated', status_reason=?, updated_at=? WHERE id=?",
                (clean_text(reason or "invalidated by agent", 300), now, kid),
            )
        else:
            raise ValueError("action must be 'verify' or 'invalidate'")
        self._fts_refresh(kid)
        return {"id": kid, "status": "active" if action == "verify" else "invalidated"}


def fts_query(words: list[str]) -> str:
    """Build a safe FTS5 OR-query from plain terms (quoted, so no operator injection)."""
    out = []
    for w in words:
        w = w.replace('"', "")
        if not w:
            continue
        out.append(f'"{w}"*' if len(w) >= 4 else f'"{w}"')
    return " OR ".join(out)


_NEGATIONS = {"not", "no", "never", "cannot", "can't", "don't", "doesn't", "isn't", "won't", "without", "disabled", "removed", "deprecated"}


def _conflicting(a: str, b: str) -> bool:
    """Heuristic: similar statements that differ in negation or in the numbers/versions they state."""
    ta, tb = set(normalize_text(a).split()), set(normalize_text(b).split())
    if bool(ta & _NEGATIONS) != bool(tb & _NEGATIONS):
        return True
    na = {t for t in ta if any(ch.isdigit() for ch in t)}
    nb = {t for t in tb if any(ch.isdigit() for ch in t)}
    return bool(na and nb and na != nb)


def _short(revision: str | None) -> str:
    return revision[:10] if revision else "working tree"
