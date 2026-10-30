"""Ranking and bounded retrieval.

Ranking combines (1) full-text relevance (SQLite FTS5 bm25), (2) query-term
coverage, (3) source authority and verification status, (4) lifecycle status,
(5) category intent inferred from the query, and (6) explicit path hints.
Results are deduplicated and cut to a character budget; every item carries its
source references so the agent can verify against the original files.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass

from .extractors import CATEGORIES
from .extractors.code import is_test_path
from .knowledge import ACTIVE_STATUSES, ALL_STATUSES, fts_query
from .util import clean_text, expand_terms, jaccard, normalize_text, term_match, terms

AUTHORITY = {
    ("extracted", "derived"): 1.0,
    ("agent", "verified"): 0.95,
    ("agent", "unverified"): 0.7,
    ("git", "historical"): 0.55,
}
STATUS_WEIGHT = {"active": 1.0, "stale": 0.55, "superseded": 0.3, "invalidated": 0.2}
INTENT = [
    ("decision", re.compile(r"\b(why|rationale|decid\w*|decision|chose|choice|tradeoffs?|trade-offs?|design (choice|decision)s?|motivation|reason)\b", re.I)),
    ("debugging", re.compile(r"\b(bug|error|fail\w*|crash\w*|broken|exception|traceback|regression|fix\w*|debug\w*|issue|problem|flaky|timeout|leak)\b", re.I)),
    ("workflow", re.compile(r"\b(deploy\w*|build\w*|release\w*|ci|pipeline|install\w*|setup|run(ning)?|test(s|ing)?|publish\w*|docker|production|serve|server|command)\b", re.I)),
    ("convention", re.compile(r"\b(convention\w*|style|lint\w*|format\w*|naming|guideline\w*|pattern|idiom\w*|consistent|best practice)\b", re.I)),
    ("dependency", re.compile(r"\b(dependenc\w*|librar\w*|package\w*|integrat\w*|sdk|version\w*|third[- ]party)\b", re.I)),
    ("architecture", re.compile(r"\b(architecture|structure|overview|component\w*|module\w*|layer\w*|boundar\w*|design|how does|works?|flow|organi[sz]ed)\b", re.I)),
    ("implementation", re.compile(r"\b(where|implement\w*|defin\w*|locat\w*|function|class|method|handler|endpoint|route)\b", re.I)),
]
_CODE_EXT = re.compile(r"\.(py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|kt|rb|php|cs|c|h|cc|cpp|hpp|swift|scala|ex|exs|vue|svelte|sql|sh)$")
_PERIPHERAL = re.compile(r"(^|/)(examples?|samples?|demos?|fixtures?|testdata|test_data|benchmarks?|playground)(/|$)", re.I)
_PATHISH = re.compile(r"[\w.-]+/[\w./-]+|[\w-]+\.(?:py|js|ts|tsx|go|rs|java|rb|md|rst|toml|json|ya?ml)\b")


@dataclass
class Ranked:
    row: sqlite3.Row
    score: float
    sources: list[dict]


def intents(query: str) -> set[str]:
    return {cat for cat, rx in INTENT if rx.search(query)}


def search(
    conn: sqlite3.Connection,
    repo_id: str,
    query: str,
    *,
    categories: list[str] | None = None,
    path_prefix: str | None = None,
    include_inactive: bool = False,
    limit: int = 10,
    pool: int = 150,
) -> tuple[list[Ranked], dict]:
    """Return ranked items plus diagnostics (matched/unmatched query terms, collapsed duplicates)."""
    qterms = terms(query)
    diag: dict = {"query_terms": qterms, "collapsed_duplicates": 0}
    if categories:
        bad = [c for c in categories if c not in CATEGORIES]
        if bad:
            raise ValueError(f"unknown categories: {', '.join(bad)} (valid: {', '.join(CATEGORIES)})")
    statuses = ALL_STATUSES if include_inactive else ACTIVE_STATUSES
    expanded = expand_terms(qterms[:32])
    weights = _term_weights(conn, repo_id, qterms[:32])
    match = fts_query(list(dict.fromkeys([*qterms[:32], *[x for t in qterms[:32] for x in expanded[t]]])))
    rows: list[sqlite3.Row] = []
    if match:
        sql = f"""SELECT k.*, bm25(knowledge_fts, 3.0, 1.0, 2.0, 1.5) AS bm FROM knowledge_fts f JOIN knowledge k ON k.id = f.kid
                 WHERE knowledge_fts MATCH ? AND f.repo_id = ? AND k.repo_id = ?
                 AND k.status IN ({','.join('?' * len(statuses))})"""
        params: list = [match, repo_id, repo_id, *statuses]
        if categories:
            sql += f" AND k.category IN ({','.join('?' * len(categories))})"
            params += categories
        sql += " ORDER BY bm LIMIT ?"
        params.append(pool)
        rows = conn.execute(sql, params).fetchall()
    if path_prefix:
        prefix = path_prefix.strip("/")
        keep = {r["knowledge_id"] for r in conn.execute(
            "SELECT DISTINCT knowledge_id FROM knowledge_sources WHERE repo_id=? AND (path = ? OR path LIKE ? ESCAPE '\\')",
            (repo_id, prefix, _like_prefix(prefix)))}
        rows = [r for r in rows if r["id"] in keep]
        if not qterms:
            rows = _rows_by_ids(conn, repo_id, list(keep)[:pool], statuses, categories)
    if not rows:
        diag["unmatched_terms"] = qterms
        return [], diag

    ids = [r["id"] for r in rows]
    sources = _sources_for(conn, repo_id, ids)
    want = intents(query)
    path_hints = [p.strip("./") for p in _PATHISH.findall(query)]
    mentions_tests = bool(re.search(r"\btests?\b|\bspec\b", query, re.I))
    best_bm = min((r["bm"] if "bm" in r.keys() else 0) for r in rows) or -1.0
    ranked: list[Ranked] = []
    matched_terms: set[str] = set()
    qset = set(qterms)
    total_weight = sum(weights.values()) or 1.0
    for r in rows:
        text_terms = set(terms(" ".join([r["title"], r["content"], r["rationale"] or "", " ".join(json.loads(r["entities"]))])))
        srcs = sources.get(r["id"], [])
        text_terms |= set(terms(" ".join(s["path"] for s in srcs)))
        hit = qset & text_terms
        # FTS uses stemming and prefixes; count inflected matches (deploy/deployment) too.
        hit |= {q for q in qset - hit if any(term_match(q, t) for t in text_terms)}
        syn_hit = {q for q in qset - hit if any(term_match(x, t) for x in expanded.get(q, ()) for t in text_terms)}
        matched_terms |= hit | syn_hit
        # Coverage weighs each query word by how rare it is in this repository's knowledge, so filler
        # ("time", "even", "keep") counts for little and specific words ("SECRET_KEY_FALLBACKS") for a lot.
        coverage = (sum(weights.get(q, 0.0) for q in hit) + 0.6 * sum(weights.get(q, 0.0) for q in syn_hit)) / total_weight
        rel = (r["bm"] / best_bm) if "bm" in r.keys() and best_bm else 0.5
        score = (0.45 * max(rel, 0.0) + 0.55 * coverage)
        score *= AUTHORITY.get((r["origin"], r["verification"]), 0.8)
        score *= STATUS_WEIGHT.get(r["status"], 0.5)
        score *= 0.5 + 0.5 * r["confidence"]
        if r["category"] in want:
            score *= 1.35
        if path_hints and any(any(s["path"].endswith(h) or s["path"].startswith(h) for h in path_hints) for s in srcs):
            score *= 1.5
        if not mentions_tests and srcs and all(is_test_path(s["path"]) for s in srcs if s["path"] != "."):
            score *= 0.6
        if "implementation" in want and any(_is_code(s["path"]) for s in srcs):
            score *= 1.4
        if srcs and all(_PERIPHERAL.search(s["path"]) for s in srcs if s["path"] != ".") and not _PERIPHERAL.search(query):
            score *= 0.7
        ranked.append(Ranked(r, score, srcs))
    ranked.sort(key=lambda x: -x.score)
    diag["unmatched_terms"] = [t for t in qterms if t not in matched_terms]
    # Return only what is clearly relevant instead of filling the budget: results far below the
    # best match are dropped (they cost the agent tokens on every later turn and add nothing).
    if ranked:
        # Judge relevance only: lifecycle status (stale/superseded) affects order, not whether an item is on topic.
        def relevance(x: Ranked) -> float:
            return x.score / STATUS_WEIGHT.get(x.row["status"], 0.5)

        floor = max(relevance(x) for x in ranked) * RELATIVE_CUTOFF
        kept = [x for x in ranked if relevance(x) >= floor]
        diag["dropped_low_relevance"] = len(ranked) - len(kept)
        ranked = kept

    # Collapse near-identical items so the budget is not spent on repetition.
    out: list[Ranked] = []
    seen_sets: list[set[str]] = []
    for item in ranked:
        tset = set(normalize_text(item.row["content"]).split())
        if any(jaccard(tset, s) >= 0.85 for s in seen_sets):
            diag["collapsed_duplicates"] += 1
            continue
        seen_sets.append(tset)
        out.append(item)
        if len(out) >= limit:
            break
    diag["candidates_considered"] = len(rows)
    return out, diag


RELATIVE_CUTOFF = 0.6


def _term_weights(conn: sqlite3.Connection, repo_id: str, qterms: list[str]) -> dict[str, float]:
    """Inverse document frequency of each query term among this repository's active knowledge."""
    n = conn.execute("SELECT COUNT(*) FROM knowledge WHERE repo_id=? AND status IN ('active','stale')", (repo_id,)).fetchone()[0]
    weights: dict[str, float] = {}
    for t in qterms:
        q = fts_query([t])
        if not q:
            continue
        df = conn.execute("SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH ? AND repo_id=?", (q, repo_id)).fetchone()[0]
        if df:
            weights[t] = math.log((n + 1) / (df + 0.5))
    return weights


def _is_code(path: str) -> bool:
    return bool(_CODE_EXT.search(path))


def _like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"


def _rows_by_ids(conn, repo_id, ids, statuses, categories) -> list[sqlite3.Row]:
    if not ids:
        return []
    sql = f"SELECT k.* FROM knowledge k WHERE k.repo_id=? AND k.id IN ({','.join('?' * len(ids))}) AND k.status IN ({','.join('?' * len(statuses))})"
    params = [repo_id, *ids, *statuses]
    if categories:
        sql += f" AND k.category IN ({','.join('?' * len(categories))})"
        params += categories
    return conn.execute(sql, params).fetchall()


def _sources_for(conn: sqlite3.Connection, repo_id: str, ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not ids:
        return out
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        q = f"""SELECT ks.knowledge_id, ks.path, ks.start_line, ks.end_line, ks.content_hash, ks.revision, ks.extract_key,
                f.content_hash AS current_hash, f.status AS file_status
                FROM knowledge_sources ks LEFT JOIN files f ON f.repo_id = ks.repo_id AND f.path = ks.path
                WHERE ks.repo_id = ? AND ks.knowledge_id IN ({','.join('?' * len(part))}) ORDER BY ks.rowid"""
        for r in conn.execute(q, [repo_id, *part]):
            out.setdefault(r["knowledge_id"], []).append(dict(r))
    return out


# ------------------------------------------------------------ serialization
def source_view(s: dict, modified_paths: set[str]) -> dict:
    if s["path"] == ".":
        return {"path": ".", "kind": "repository structure (derived from the file index)"}
    v: dict = {"path": s["path"]}
    if s.get("start_line"):
        v["lines"] = f"{s['start_line']}-{s['end_line']}" if s.get("end_line") and s["end_line"] != s["start_line"] else str(s["start_line"])
    if s.get("extract_key", "") and str(s.get("extract_key")).startswith("commit:"):
        v["commit"] = (s.get("revision") or "")[:10]
        return v
    if s.get("current_hash") is None:
        v["evidence"] = "source no longer indexed"
    elif s.get("content_hash") and s["content_hash"] != s["current_hash"]:
        v["evidence"] = "source changed since recorded"
    elif s["path"] in modified_paths:
        v["evidence"] = "file modified in working tree since last index"
    return v


COMPACT_CONTENT_CHARS = 420


def item_view(r: sqlite3.Row, sources: list[dict], modified_paths: set[str], *, compact: bool = True, max_sources: int = 4) -> dict:
    content, rationale = r["content"], r["rationale"]
    if compact:
        content = clean_text(content, COMPACT_CONTENT_CHARS)
        rationale = clean_text(rationale, 300) if rationale else None
    v: dict = {
        "id": r["id"],
        "category": r["category"],
        "title": r["title"],
        "content": content,
    }
    if rationale:
        v["rationale"] = rationale
    if compact and (content != r["content"] or rationale != r["rationale"]):
        v["shortened"] = True
    real_sources = [s for s in sources]
    v["sources"] = [source_view(s, modified_paths) for s in real_sources[:max_sources]]
    if len(real_sources) > max_sources:
        v["more_sources"] = len(real_sources) - max_sources
    v["provenance"] = f"{r['origin']}/{r['verification']}"
    if r["status"] != "active" or not compact:
        v["status"] = r["status"]
    if r["status_reason"]:
        v["status_reason"] = r["status_reason"]
    if r["verified_revision"]:
        v["as_of"] = r["verified_revision"][:10]
    if not compact:
        v.update(
            entities=json.loads(r["entities"]),
            confidence=r["confidence"],
            observations=r["observations"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            verified_at=r["verified_at"],
            verified_revision=r["verified_revision"],
            superseded_by=r["superseded_by"],
            origin=r["origin"],
            verification=r["verification"],
            extractor=r["extractor"],
        )
    return v


def fit_budget(items: list[dict], max_chars: int) -> tuple[list[dict], int]:
    """Keep items in order until the serialized size reaches ``max_chars``; shorten the last one if needed."""
    out, used = [], 2
    for it in items:
        size = len(json.dumps(it, ensure_ascii=False, separators=(",", ":"))) + 1
        if used + size > max_chars:
            room = max_chars - used - (size - len(it.get("content", "")))
            if not out and room > 80:
                it = dict(it, content=clean_text(it["content"], room), truncated=True)
                out.append(it)
                used += len(json.dumps(it, ensure_ascii=False, separators=(",", ":")))
            break
        out.append(it)
        used += size
    return out, len(items) - len(out)


def search_symbols(conn: sqlite3.Connection, repo_id: str, query: str, limit: int = 10) -> list[dict]:
    """Symbols whose name, or what their body calls/catches/reads, matches the query.

    A query term counts 1.5 when it matches the symbol's name, 1.0 when it matches a reference in its
    body, and 0.6 when only a synonym matches. At most three results come from the same class or module,
    so one big class cannot crowd out the function that actually does the work.
    """
    qterms = terms(query)
    expanded = expand_terms(qterms)
    words = list(dict.fromkeys([*qterms[:24], *[s for t in qterms[:24] for s in expanded[t]]]))
    match = fts_query(words)
    if not match:
        return []
    rows = conn.execute(
        """SELECT path, name, kind, line, signature, refs, bm25(symbols_fts, 2.0, 1.0) AS bm FROM symbols_fts
           WHERE symbols_fts MATCH ? AND repo_id = ? ORDER BY bm LIMIT ?""",
        (match, repo_id, max(limit * 20, 200)),
    ).fetchall()
    mentions_tests = bool(re.search(r"\btests?\b", query, re.I))
    scored = []
    for r in rows:
        nterms = set(terms(r["name"]))
        rterms = set(terms(r["refs"] or ""))
        score, name_hit = 0.0, False
        for q in qterms:
            syns = expanded[q]
            if any(term_match(q, t) for t in nterms):
                score += 1.5
                name_hit = True
            elif any(term_match(q, t) for t in rterms):
                score += 1.0
            elif any(term_match(s, t) for s in syns for t in nterms | rterms):
                score += 0.6
        if score < 1.0:
            continue  # matched only through the file path or a lone synonym: not evidence of relevance
        if r["kind"] in ("class", "function", "type", "route", "entrypoint"):
            score *= 1.2
        if not name_hit:
            score *= 0.9
        if not mentions_tests and is_test_path(r["path"]):
            score *= 0.3
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1]["path"], x[1]["line"]))
    out: list[dict] = []
    per_owner: dict[tuple[str, str], int] = {}
    for _, r in scored:
        owner = (r["path"], r["name"].split(".")[0] if "." in r["name"] else "")
        if per_owner.get(owner, 0) >= 3:
            continue
        per_owner[owner] = per_owner.get(owner, 0) + 1
        out.append({"symbol": r["name"], "kind": r["kind"], "path": r["path"], "line": int(r["line"]), "signature": r["signature"]})
        if len(out) >= limit:
            break
    return out
