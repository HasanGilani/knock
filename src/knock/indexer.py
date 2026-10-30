"""Repository registration, indexing and freshness.

Indexing is incremental and resumable:

* A file is re-read only when its size/mtime changed, and re-extracted only when its
  content hash (or the extractor version) changed.
* Every file is committed in its own transaction, so an interrupted run loses at most
  the file in flight; the next run simply continues from the stored hashes.
* A run is marked ``completed`` and the repository's ``indexed_revision`` advanced only
  after every discovered file and deletion has been processed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import gitutil, logs
from .config import Config
from .db import Tx
from .discovery import discover
from .extractors import EXTRACTOR_VERSION, Candidate, extract_file
from .extractors import repo as repo_extractor
from .knowledge import KnowledgeStore, SyncStats
from .metrics import Metrics
from .security import contains_private_key, redact
from .util import first_sentences, sha256_hex, terms, utcnow

log = logs.get("indexer")

REPO_SCOPE = "."           # pseudo-path for repository-level knowledge
RUN_STALE_SECONDS = 15 * 60
_FIX_COMMIT = re.compile(r"(?i)\b(fix(e[sd])?|bug(fix)?|hotfix|regression|revert(s|ed)?|crash(es)?|workaround|resolve[sd]?|patch(ed)?)\b")


class IndexBusy(RuntimeError):
    pass


@dataclass
class Repo:
    id: str
    name: str
    root: Path
    indexed_revision: str | None
    last_indexed_at: str | None
    generation: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Repo":
        return cls(row["id"], row["name"], Path(row["root_path"]), row["indexed_revision"], row["last_indexed_at"], row["generation"])


# ----------------------------------------------------------------- registry
def register(conn: sqlite3.Connection, path: str | os.PathLike[str]) -> Repo:
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ValueError(f"not a directory: {p}")
    root = gitutil.toplevel(p) or p
    root = Path(os.path.realpath(root))
    existing = conn.execute("SELECT * FROM repositories WHERE root_path = ?", (str(root),)).fetchone()
    if existing:
        return Repo.from_row(existing)
    repo_id = "r_" + sha256_hex(str(root))[:12]
    name = root.name
    if conn.execute("SELECT 1 FROM repositories WHERE name = ?", (name,)).fetchone():
        name = f"{name}-{repo_id[2:8]}"
    conn.execute(
        "INSERT INTO repositories(id, name, root_path, created_at) VALUES (?,?,?,?)",
        (repo_id, name, str(root), utcnow()),
    )
    log.info("registered repository", extra={"repo": name, "root": str(root)})
    return Repo.from_row(conn.execute("SELECT * FROM repositories WHERE id = ?", (repo_id,)).fetchone())


def find_repo(conn: sqlite3.Connection, ref: str) -> Repo | None:
    row = conn.execute("SELECT * FROM repositories WHERE id = ? OR name = ?", (ref, ref)).fetchone()
    if row is None and ref:
        p = Path(ref).expanduser()
        if p.exists():
            root = gitutil.toplevel(p) if p.is_dir() else None
            real = os.path.realpath(root or p)
            row = conn.execute("SELECT * FROM repositories WHERE root_path = ?", (real,)).fetchone()
            if row is None:
                # A path inside a registered repository.
                for r in conn.execute("SELECT * FROM repositories"):
                    if real == r["root_path"] or real.startswith(r["root_path"].rstrip(os.sep) + os.sep):
                        return Repo.from_row(r)
    return Repo.from_row(row) if row else None


def list_repos(conn: sqlite3.Connection) -> list[Repo]:
    return [Repo.from_row(r) for r in conn.execute("SELECT * FROM repositories ORDER BY name")]


def unregister(conn: sqlite3.Connection, repo_id: str) -> None:
    """Delete everything knock stored about a repository (never touches the repository itself)."""
    with Tx(conn):
        ids = [r["id"] for r in conn.execute("SELECT id FROM knowledge WHERE repo_id=?", (repo_id,))]
        conn.execute("DELETE FROM knowledge_fts WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM symbols_fts WHERE repo_id=?", (repo_id,))
        for table in ("knowledge_sources", "symbols", "commits", "commit_files", "query_cache", "metrics", "files", "index_runs"):
            conn.execute(f"DELETE FROM {table} WHERE repo_id=?", (repo_id,))
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            conn.execute(f"DELETE FROM knowledge_links WHERE src_id IN ({','.join('?' * len(part))})", part)
        conn.execute("DELETE FROM knowledge WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM repositories WHERE id=?", (repo_id,))


def bump_generation(conn: sqlite3.Connection, repo_id: str) -> None:
    """Any change to a repository's knowledge invalidates its cached query results."""
    conn.execute("UPDATE repositories SET generation = generation + 1 WHERE id = ?", (repo_id,))
    conn.execute("DELETE FROM query_cache WHERE repo_id = ?", (repo_id,))


# ------------------------------------------------------------------ indexing
class Indexer:
    def __init__(self, conn: sqlite3.Connection, config: Config):
        self.conn = conn
        self.config = config
        self.store = KnowledgeStore(conn)
        self.metrics = Metrics(conn)

    def run(self, repo: Repo, full: bool = False) -> dict:
        if not repo.root.is_dir():
            raise ValueError(f"repository root no longer exists: {repo.root}")
        run_id = self._acquire(repo, "full" if full else "incremental")
        started = time.monotonic()
        stats: dict = {
            "files_discovered": 0, "files_unchanged": 0, "files_added": 0, "files_modified": 0,
            "files_deleted": 0, "files_renamed": 0, "files_skipped_discovery": 0, "files_skipped_content": 0,
            "files_error": 0, "secrets_redacted": 0, "agent_items_marked_stale": 0, "commits_ingested": 0,
            "skip_reasons": {}, "truncated": False,
        }
        k = SyncStats()
        try:
            revision = gitutil.head_revision(repo.root)
            files, skipped, truncated = discover(repo.root, self.config)
            stats["files_discovered"] = len(files)
            stats["files_skipped_discovery"] = len(skipped)
            stats["truncated"] = truncated
            reasons: dict[str, int] = {}
            for s in skipped:
                key = re.sub(r"\s*\(.*\)$", "", s.reason)
                reasons[key] = reasons.get(key, 0) + 1
            stats["skip_reasons"] = reasons

            known = {r["path"]: r for r in self.conn.execute("SELECT * FROM files WHERE repo_id = ?", (repo.id,))}
            seen: set[str] = set()
            new_by_hash: dict[str, str] = {}
            for n, f in enumerate(files):
                seen.add(f.path)
                if n % 50 == 0:
                    self._heartbeat(run_id, stats)
                row = known.get(f.path)
                if (
                    not full and row is not None and row["status"] != "error"
                    and row["size"] == f.size and row["mtime_ns"] == f.mtime_ns
                    and row["extractor_version"] == EXTRACTOR_VERSION and row["language"] == f.language
                ):
                    stats["files_unchanged"] += 1
                    continue
                try:
                    data = _read_nofollow(repo.root / f.path)
                except OSError as exc:
                    self._record_file_error(repo, f, revision, f"read failed: {exc.strerror or exc}", k)
                    stats["files_error"] += 1
                    continue
                h = sha256_hex(data)
                if (
                    not full and row is not None and row["content_hash"] == h and row["status"] != "error"
                    and row["extractor_version"] == EXTRACTOR_VERSION and row["language"] == f.language
                ):
                    self.conn.execute("UPDATE files SET size=?, mtime_ns=? WHERE repo_id=? AND path=?", (f.size, f.mtime_ns, repo.id, f.path))
                    stats["files_unchanged"] += 1
                    continue
                if row is None:
                    stats["files_added"] += 1
                    new_by_hash.setdefault(h, f.path)
                elif row["content_hash"] != h:
                    stats["files_modified"] += 1
                outcome = self._process(repo, f, data, h, row, revision, k, stats)
                if outcome == "skipped":
                    stats["files_skipped_content"] += 1
                elif outcome == "error":
                    stats["files_error"] += 1
                elif outcome.startswith("indexed:"):
                    stats["secrets_redacted"] += int(outcome.split(":")[1])

            # Deletions (and renames: a vanished path whose content reappeared elsewhere).
            for path, row in known.items():
                if path in seen:
                    continue
                with Tx(self.conn):
                    new_path = new_by_hash.get(row["content_hash"] or "")
                    if new_path:
                        self.store.rename_path(repo.id, path, new_path)
                        stats["files_renamed"] += 1
                    else:
                        stats["agent_items_marked_stale"] += self.store.mark_path_changed(repo.id, path, None, revision)
                    k.add(self.store.sync_extracted(repo.id, path, [], None, revision))
                    self._delete_file(repo.id, path)
                    stats["files_deleted"] += 1

            changed = any(stats[x] for x in ("files_added", "files_modified", "files_deleted", "files_error")) or full
            if changed or self._repo_scope_missing(repo.id):
                with Tx(self.conn):
                    k.add(self._derive_repo_level(repo, revision))
            backfill = self.conn.execute(
                "SELECT 1 FROM commits WHERE repo_id=? AND body IS NULL LIMIT 1", (repo.id,)).fetchone() is not None
            if revision and revision != repo.indexed_revision or full or backfill:
                since = None if full or backfill else repo.indexed_revision
                stats["commits_ingested"] = self._ingest_commits(repo, revision, since)

            stats.update(
                knowledge_created=k.created, knowledge_updated=k.updated, knowledge_unchanged=k.unchanged,
                knowledge_deduplicated=k.deduplicated, knowledge_invalidated=k.invalidated,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            with Tx(self.conn):
                self.conn.execute(
                    "UPDATE repositories SET indexed_revision=?, last_indexed_at=? WHERE id=?",
                    (revision, utcnow(), repo.id),
                )
                if changed or stats["commits_ingested"] or k.created or k.updated or k.invalidated or k.deduplicated:
                    bump_generation(self.conn, repo.id)
                self.conn.execute(
                    "UPDATE index_runs SET status='completed', finished_at=?, heartbeat_at=?, revision=?, stats=? WHERE id=?",
                    (utcnow(), utcnow(), revision, json.dumps(stats), run_id),
                )
                self._record_metrics(repo.id, stats)
            log.info("index run completed", extra={"repo": repo.name, "mode": "full" if full else "incremental", **{x: stats[x] for x in ("files_added", "files_modified", "files_deleted", "files_unchanged", "duration_ms")}})
            return {"run_id": run_id, "status": "completed", "revision": revision, **stats}
        except BaseException as exc:
            status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
            try:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                self.conn.execute(
                    "UPDATE index_runs SET status=?, finished_at=?, error=?, stats=? WHERE id=?",
                    (status, utcnow(), f"{type(exc).__name__}: {exc}"[:500], json.dumps(stats), run_id),
                )
                # Partially processed files are committed and consistent; cached answers may not be.
                bump_generation(self.conn, repo.id)
            except sqlite3.Error:  # pragma: no cover - best effort
                pass
            log.error("index run %s", status, extra={"repo": repo.name, "error": str(exc)})
            raise

    # ------------------------------------------------------------- per file
    def _process(self, repo: Repo, f, data: bytes, h: str, row, revision: str | None, k: SyncStats, stats: dict) -> str:
        reason = None
        text = ""
        if b"\0" in data[:8192]:
            reason = "binary content"
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                reason = "not UTF-8 text"
        if reason is None and contains_private_key(text):
            reason = "contains a private key"
        if reason is not None:
            with Tx(self.conn):
                k.add(self.store.sync_extracted(repo.id, f.path, [], h, revision))
                if row is not None:
                    stats["agent_items_marked_stale"] += self.store.mark_path_changed(repo.id, f.path, None, revision)
                self._replace_symbols(repo.id, f.path, [])
                self._upsert_file(repo.id, f, h, "skipped", reason, 0, revision, None)
            return "skipped"
        redacted, n_redacted = redact(text)
        try:
            ex = extract_file(f.path, redacted, f.language, f.kind)
        except Exception as exc:  # an extractor bug must not abort the whole run
            log.warning("extraction failed", extra={"path": f.path, "error": repr(exc)})
            stats["agent_items_marked_stale"] += self._record_file_error(repo, f, revision, f"extraction failed: {type(exc).__name__}", k, h)
            return "error"
        with Tx(self.conn):
            k.add(self.store.sync_extracted(repo.id, f.path, ex.candidates, h, revision))
            if row is not None and row["content_hash"] != h:
                stats["agent_items_marked_stale"] += self.store.mark_path_changed(repo.id, f.path, h, revision)
            self._replace_symbols(repo.id, f.path, ex.symbols)
            summary = first_sentences(ex.doc, 200) if ex.doc else None
            self._upsert_file(repo.id, f, h, "indexed", None, n_redacted, revision, summary)
        return f"indexed:{n_redacted}"

    def _record_file_error(self, repo: Repo, f, revision: str | None, reason: str, k: SyncStats, h: str | None = None) -> int:
        with Tx(self.conn):
            k.add(self.store.sync_extracted(repo.id, f.path, [], h, revision))
            stale = self.store.mark_path_changed(repo.id, f.path, None, revision)
            self._replace_symbols(repo.id, f.path, [])
            self._upsert_file(repo.id, f, h, "error", reason, 0, revision, None)
        return stale

    def _upsert_file(self, repo_id: str, f, h: str | None, status: str, reason: str | None, redactions: int, revision: str | None, summary: str | None) -> None:
        self.conn.execute(
            """INSERT INTO files(repo_id, path, content_hash, size, mtime_ns, language, kind, status, skip_reason, summary,
               redactions, extractor_version, indexed_at, indexed_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(repo_id, path) DO UPDATE SET content_hash=excluded.content_hash, size=excluded.size,
               mtime_ns=excluded.mtime_ns, language=excluded.language, kind=excluded.kind, status=excluded.status,
               skip_reason=excluded.skip_reason, summary=excluded.summary, redactions=excluded.redactions,
               extractor_version=excluded.extractor_version, indexed_at=excluded.indexed_at, indexed_revision=excluded.indexed_revision""",
            (repo_id, f.path, h, f.size, f.mtime_ns, f.language, f.kind, status, reason, summary, redactions, EXTRACTOR_VERSION, utcnow(), revision),
        )

    def _replace_symbols(self, repo_id: str, path: str, symbols) -> None:
        self.conn.execute("DELETE FROM symbols WHERE repo_id=? AND path=?", (repo_id, path))
        self.conn.execute("DELETE FROM symbols_fts WHERE repo_id=? AND path=?", (repo_id, path))
        for s in symbols[:2000]:
            self.conn.execute("INSERT INTO symbols(repo_id, path, name, kind, line, signature) VALUES (?,?,?,?,?,?)", (repo_id, path, s.name, s.kind, s.line, s.signature))
            refs = getattr(s, "refs", None) or []
            self.conn.execute(
                "INSERT INTO symbols_fts(terms, refs, repo_id, path, name, kind, line, signature) VALUES (?,?,?,?,?,?,?,?)",
                (" ".join([s.name, *terms(s.name), *terms(path)]), " ".join([*refs, *terms(" ".join(refs))]),
                 repo_id, path, s.name, s.kind, s.line, s.signature),
            )

    def _delete_file(self, repo_id: str, path: str) -> None:
        self._replace_symbols(repo_id, path, [])
        self.conn.execute("DELETE FROM files WHERE repo_id=? AND path=?", (repo_id, path))

    # ------------------------------------------------------- repo-level
    def _repo_scope_missing(self, repo_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM knowledge_sources WHERE repo_id=? AND path=? LIMIT 1", (repo_id, REPO_SCOPE)
        ).fetchone() is None and self.conn.execute("SELECT 1 FROM files WHERE repo_id=? AND status='indexed' LIMIT 1", (repo_id,)).fetchone() is not None

    def _derive_repo_level(self, repo: Repo, revision: str | None) -> SyncStats:
        files = [dict(r) for r in self.conn.execute("SELECT path, language, kind, size, summary FROM files WHERE repo_id=? AND status='indexed'", (repo.id,))]
        symbols = [dict(r) for r in self.conn.execute("SELECT path, name, kind FROM symbols WHERE repo_id=?", (repo.id,))]
        docs = {f["path"]: f["summary"] for f in files if f["summary"]}
        deps: set[str] = set()
        for r in self.conn.execute("SELECT k.entities FROM knowledge k WHERE k.repo_id=? AND k.category='dependency' AND k.status='active'", (repo.id,)):
            deps.update(e.lower() for e in json.loads(r["entities"]))
        cands, extra = repo_extractor.derive(files, symbols, docs, deps)
        digest = sha256_hex("\n".join(c.content for c in cands))
        return self.store.sync_extracted(repo.id, REPO_SCOPE, cands, digest, revision, extractor="repo-structure", extra_paths=extra)

    def _ingest_commits(self, repo: Repo, revision: str | None, since: str | None) -> int:
        commits = gitutil.recent_commits(repo.root, self.config.commit_history_limit, since)
        if not commits:
            return 0
        added = 0
        with Tx(self.conn):
            for c in commits:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO commits(repo_id, sha, committed_at, subject, files, body) VALUES (?,?,?,?,?,?)",
                    (repo.id, c.sha, c.committed_at, c.subject, json.dumps(c.files), c.body),
                )
                if not cur.rowcount:
                    # Stored before commit bodies were kept: add the body and rebuild its knowledge.
                    upd = self.conn.execute("UPDATE commits SET body=? WHERE repo_id=? AND sha=? AND body IS NULL", (c.body, repo.id, c.sha))
                    if upd.rowcount and _FIX_COMMIT.search(c.subject):
                        self._delete_commit_knowledge(repo.id, c.sha)
                        self._commit_knowledge(repo.id, c)
                    continue
                added += 1
                for path in c.files:
                    self.conn.execute("INSERT INTO commit_files(repo_id, sha, path) VALUES (?,?,?)", (repo.id, c.sha, path))
                if _FIX_COMMIT.search(c.subject):
                    self._commit_knowledge(repo.id, c)
            self._prune_commits(repo.id)
        return added

    def _commit_knowledge(self, repo_id: str, c: gitutil.Commit) -> None:
        shown = ", ".join(c.files[:8]) + (f" (+{len(c.files) - 8} more)" if len(c.files) > 8 else "")
        # The body usually explains *why*; keep it (trailers and emails were stripped by gitutil).
        why = redact(re.sub(r"\s+", " ", c.body).strip())[0] if c.body else None
        cand = Candidate(
            key=f"commit:{c.sha}", category="debugging", title=f"Fix commit {c.sha[:10]}: {c.subject[:120]}",
            content=f"Commit {c.sha[:10]} ({c.committed_at[:10]}): {c.subject}." + (f" Files changed: {shown}." if shown else ""),
            rationale=first_sentences(why, 700) if why else None,
            entities=["commit", "fix", *[p.split('/')[-1] for p in c.files[:8]]], confidence=0.6,
        )
        from .knowledge import fingerprint
        kid = self.store._insert_item(repo_id, cand, fingerprint(cand.category, cand.content + c.sha), "git", "git-log", "historical", c.sha, utcnow())
        for path in c.files[:20]:
            self.conn.execute(
                "INSERT INTO knowledge_sources(knowledge_id, repo_id, path, extract_key, start_line, end_line, content_hash, revision) VALUES (?,?,?,?,?,?,?,?)",
                (kid, repo_id, path, f"commit:{c.sha}", None, None, None, c.sha),
            )
        self.store._fts_refresh(kid)

    def _prune_commits(self, repo_id: str) -> None:
        limit = self.config.commit_history_limit
        old = [r["sha"] for r in self.conn.execute(
            "SELECT sha FROM commits WHERE repo_id=? ORDER BY committed_at DESC LIMIT -1 OFFSET ?", (repo_id, limit))]
        for sha in old:
            self.conn.execute("DELETE FROM commits WHERE repo_id=? AND sha=?", (repo_id, sha))
            self.conn.execute("DELETE FROM commit_files WHERE repo_id=? AND sha=?", (repo_id, sha))
            self._delete_commit_knowledge(repo_id, sha)

    def _delete_commit_knowledge(self, repo_id: str, sha: str) -> None:
        for r in self.conn.execute("SELECT DISTINCT knowledge_id FROM knowledge_sources WHERE repo_id=? AND extract_key=?", (repo_id, f"commit:{sha}")).fetchall():
            self.conn.execute("DELETE FROM knowledge_fts WHERE kid=?", (r["knowledge_id"],))
            self.conn.execute("DELETE FROM knowledge WHERE id=? AND origin='git'", (r["knowledge_id"],))

    # ---------------------------------------------------------- run lock
    def _acquire(self, repo: Repo, mode: str) -> int:
        with Tx(self.conn):
            for r in self.conn.execute("SELECT * FROM index_runs WHERE repo_id=? AND status='running'", (repo.id,)).fetchall():
                age = time.time() - _parse_ts(r["heartbeat_at"])
                if _pid_alive(r["pid"]) and age < RUN_STALE_SECONDS:
                    raise IndexBusy(f"an index run for {repo.name} is already in progress (pid {r['pid']})")
                self.conn.execute(
                    "UPDATE index_runs SET status='interrupted', finished_at=?, error=? WHERE id=?",
                    (utcnow(), "process ended before the run completed; resumed by a later run", r["id"]),
                )
            cur = self.conn.execute(
                "INSERT INTO index_runs(repo_id, mode, status, pid, started_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
                (repo.id, mode, "running", os.getpid(), utcnow(), utcnow()),
            )
            return int(cur.lastrowid)

    def _heartbeat(self, run_id: int, stats: dict) -> None:
        self.conn.execute("UPDATE index_runs SET heartbeat_at=?, stats=? WHERE id=?", (utcnow(), json.dumps(stats), run_id))

    def _record_metrics(self, repo_id: str, stats: dict) -> None:
        m = self.metrics
        m.incr(repo_id, "index_runs")
        m.incr(repo_id, "files_indexed_new", stats["files_added"])
        m.incr(repo_id, "files_reprocessed", stats["files_modified"])
        m.incr(repo_id, "files_unchanged_skipped", stats["files_unchanged"])
        m.incr(repo_id, "files_deleted", stats["files_deleted"])
        m.incr(repo_id, "files_error", stats["files_error"])
        m.incr(repo_id, "knowledge_created", stats["knowledge_created"])
        m.incr(repo_id, "knowledge_updated", stats["knowledge_updated"])
        m.incr(repo_id, "knowledge_deduplicated", stats["knowledge_deduplicated"])
        m.incr(repo_id, "knowledge_invalidated", stats["knowledge_invalidated"])
        m.incr(repo_id, "knowledge_marked_stale", stats["agent_items_marked_stale"])
        m.incr(repo_id, "secrets_redacted", stats["secrets_redacted"])
        m.observe(repo_id, "index_duration_ms", stats["duration_ms"])
        # Deterministic extraction makes no model calls; recorded so cost reports are explicit.
        m.incr(repo_id, "extraction_model_calls", 0)


# ---------------------------------------------------------------- freshness
_fresh_cache: dict[str, tuple[float, int, dict]] = {}


def freshness(conn: sqlite3.Connection, repo: Repo, config: Config) -> dict:
    """Cheap check of how current the index is: HEAD vs indexed revision, plus a stat() scan."""
    cached = _fresh_cache.get(repo.id)
    if cached and time.monotonic() - cached[0] < config.freshness_ttl_seconds and cached[1] == repo.generation:
        return cached[2]
    run = conn.execute("SELECT * FROM index_runs WHERE repo_id=? ORDER BY id DESC LIMIT 1", (repo.id,)).fetchone()
    head = gitutil.head_revision(repo.root) if repo.root.is_dir() else None
    modified, deleted, checked = [], [], 0
    for r in conn.execute("SELECT path, size, mtime_ns FROM files WHERE repo_id=? AND status != 'error' LIMIT ?", (repo.id, config.freshness_stat_limit)):
        checked += 1
        try:
            st = os.lstat(repo.root / r["path"])
        except FileNotFoundError:
            deleted.append(r["path"])
            continue
        except OSError:
            continue
        if st.st_size != r["size"] or st.st_mtime_ns != r["mtime_ns"]:
            modified.append(r["path"])
    stale_items = conn.execute("SELECT COUNT(*) FROM knowledge WHERE repo_id=? AND status='stale'", (repo.id,)).fetchone()[0]
    if repo.last_indexed_at is None:
        state = "indexing" if run and run["status"] == "running" else "never_indexed"
    elif run and run["status"] == "running":
        state = "indexing"
    elif run and run["status"] in ("failed", "interrupted"):
        state = "incomplete"
    elif (head and head != repo.indexed_revision) or modified or deleted:
        state = "stale"
    else:
        state = "fresh"
    result = {
        "state": state,
        "indexed_revision": repo.indexed_revision,
        "head_revision": head,
        "head_moved": bool(head and repo.indexed_revision and head != repo.indexed_revision),
        "last_indexed_at": repo.last_indexed_at,
        "files_modified_since_index": len(modified),
        "files_deleted_since_index": len(deleted),
        "modified_paths": (modified + deleted)[:20],
        "stale_knowledge_items": stale_items,
        "files_checked": checked,
        "last_run": {"status": run["status"], "mode": run["mode"], "finished_at": run["finished_at"], "error": run["error"]} if run else None,
        "note": "New untracked files are detected by refresh_repository, not by this check.",
    }
    if state in ("stale", "incomplete"):
        result["advice"] = "Call refresh_repository (incremental, deterministic, no model calls) or verify with get_source."
    _fresh_cache[repo.id] = (time.monotonic(), repo.generation, result)
    return result


def reset_freshness_cache() -> None:
    _fresh_cache.clear()


# ------------------------------------------------------------------- helpers
def _read_nofollow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parse_ts(ts: str) -> float:
    import calendar

    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
