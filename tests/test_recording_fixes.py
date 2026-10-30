"""Regression tests for problems found in recorded Claude Code sessions.

1. Commit messages were reduced to their subject line, losing the explanation of *why*.
2. Code could only be found by its name, not by what it does (calls, catches, reads).
3. Results filled the budget with off-topic items instead of stopping at what is relevant.
"""

import sqlite3

from knock import db, retrieval
from knock.gitutil import clean_body

from .conftest import git, write


def test_commit_body_kept_without_trailers_or_emails():
    body = (
        "The serializer expects the active key last.\n\n"
        "Contact jane@example.com for details.\n"
        "Signed-off-by: Jane Doe <jane@example.com>\n"
        "Co-authored-by: Bob <bob@example.org>\n"
    )
    out = clean_body(body)
    assert "active key last" in out
    assert "@" not in out and "Signed-off-by" not in out and "Co-authored-by" not in out


def test_fix_commit_knowledge_carries_the_why(indexed):
    svc, root, name = indexed
    write(root, "src/orders/pricing.py", (root / "src/orders/pricing.py").read_text().replace("> 100", ">= 100"))
    git(root, "commit", "-qam", "Fix discount threshold off-by-one\n\nOrders of exactly 100 were not discounted because "
        "the comparison was strict.\n\nSigned-off-by: Dev <dev@example.com>")
    svc.refresh_repository(name)
    hit = next(r for r in svc.search_knowledge("discount threshold exactly 100", repository=name)["results"]
               if r["provenance"] == "git/historical")
    assert "comparison was strict" in hit["rationale"]
    assert "dev@example.com" not in str(hit)
    row = svc.conn.execute("SELECT body FROM commits WHERE subject LIKE 'Fix discount%'").fetchone()
    assert "comparison was strict" in row["body"]


def test_old_index_backfills_commit_bodies(indexed):
    svc, root, name = indexed
    write(root, "README.md", (root / "README.md").read_text() + "\nMore.\n")
    git(root, "commit", "-qam", "Fix README typo\n\nThe install section had a broken command.")
    svc.refresh_repository(name)
    # Simulate an index built before bodies were stored.
    svc.conn.execute("UPDATE commits SET body = NULL")
    svc.conn.execute("DELETE FROM knowledge WHERE origin = 'git'")
    result = svc.refresh_repository(name)
    assert result["files_modified"] == 0
    assert svc.conn.execute("SELECT COUNT(*) FROM commits WHERE body IS NULL").fetchone()[0] == 0
    item = svc.conn.execute("SELECT rationale FROM knowledge WHERE origin='git' AND title LIKE '%README typo%'").fetchone()
    assert item is not None and "broken command" in item["rationale"]


def test_function_found_by_what_it_catches_and_reads(make_repo, service):
    root = make_repo({
        "README.md": "# Auth\n\nA tiny service used to test symbol search by behaviour.\n",
        "app/sessions.py": (
            '"""Session handling."""\n\n'
            "class CookieStore:\n"
            "    def load(self, request):\n"
            "        try:\n"
            "            return self.serializer.loads(request.cookies['session'])\n"
            "        except BadSignature:\n"
            "            return None\n\n"
            "    def keys(self, app):\n"
            "        return [*app.config['SECRET_KEY_FALLBACKS'], app.secret_key]\n"
        ),
    })
    rid = service.register(str(root))["id"]
    service.refresh_repository(rid)
    repo = service.resolve(rid)
    found = retrieval.search_symbols(service.conn, repo.id, "where is a cookie with a bad signature handled", 5)
    assert found and found[0]["symbol"] == "CookieStore.load"
    found = retrieval.search_symbols(service.conn, repo.id, "what reads SECRET_KEY_FALLBACKS", 5)
    assert found and found[0]["symbol"] == "CookieStore.keys"
    # A synonym bridges "verified" to the signature check.
    found = retrieval.search_symbols(service.conn, repo.id, "session cookie cannot be verified", 5)
    assert "CookieStore.load" in [f["symbol"] for f in found]


def test_low_relevance_results_are_dropped(indexed):
    svc, root, name = indexed
    repo = svc.resolve(name)
    ranked, diag = retrieval.search(svc.conn, repo.id, "why did we choose PostgreSQL for order storage", limit=25)
    assert ranked[0].row["category"] == "decision"
    assert diag["dropped_low_relevance"] > 0
    top = ranked[0].score
    assert all(r.score >= top * retrieval.RELATIVE_CUTOFF * 0.3 for r in ranked)  # status weight can lower order, not relevance


def test_v1_database_upgrades(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    for statement in db._split_sql(db.MIGRATIONS[0]):
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO repositories(id, name, root_path, created_at) VALUES ('r_1','x','/x','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO commits(repo_id, sha, committed_at, subject, files) VALUES ('r_1','abc','2026-01-01','s','[]')")
    conn.execute("INSERT INTO files(repo_id, path, size, mtime_ns, status, extractor_version, indexed_at) VALUES ('r_1','a.py',1,1,'indexed',3,'t')")
    conn.close()
    upgraded = db.connect(path)
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert upgraded.execute("SELECT body FROM commits").fetchone()["body"] is None        # backfilled on next refresh
    assert upgraded.execute("SELECT extractor_version FROM files").fetchone()[0] == 0     # forces re-extraction
    upgraded.execute("INSERT INTO symbols_fts(terms, refs, repo_id, path, name, kind, line, signature) VALUES ('a','b','r_1','a.py','f','function',1,'')")
    upgraded.close()
