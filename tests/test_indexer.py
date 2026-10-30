import json
import os

import pytest

from knock import indexer
from knock.extractors import code as code_extractor

from .conftest import AWS_KEY, STRIPE_SECRET, git, write


def active_titles(svc, repo_id):
    return {r["title"]: r["status"] for r in svc.conn.execute("SELECT title, status FROM knowledge WHERE repo_id=?", (repo_id,))}


def test_initial_index_builds_knowledge(indexed):
    svc, root, name = indexed
    repo = svc.resolve(name)
    status = svc.repository_status(name)
    assert status["counts"]["files"]["indexed"] >= 12
    cats = status["counts"]["active_knowledge_by_category"]
    for c in ("architecture", "decision", "dependency", "workflow", "convention", "implementation", "debugging"):
        assert cats.get(c, 0) >= 1, c
    assert repo.indexed_revision == git(root, "rev-parse", "HEAD").strip()
    assert status["freshness"]["state"] == "fresh"


def test_reindex_without_changes_does_nothing(indexed):
    svc, root, name = indexed
    gen = svc.resolve(name).generation
    result = svc.refresh_repository(name)
    assert result["files_added"] == result["files_modified"] == result["files_deleted"] == 0
    assert result["files_unchanged"] == result["files_discovered"]
    assert result["knowledge_created"] == result["knowledge_updated"] == result["knowledge_invalidated"] == 0
    assert svc.resolve(name).generation == gen   # caches stay valid


def test_touch_without_content_change_is_not_reprocessed(indexed):
    svc, root, name = indexed
    p = root / "README.md"
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5_000_000_000))
    result = svc.refresh_repository(name)
    assert result["files_modified"] == 0 and result["knowledge_created"] == 0


def test_modification_is_incremental_and_updates_knowledge(indexed):
    svc, root, name = indexed
    repo = svc.resolve(name)
    before = svc.search_knowledge("deployment kubectl", repository=name)["results"]
    dep = next(r for r in before if r["title"].endswith("Deployment"))
    text = (root / "README.md").read_text().replace("rolled out with `kubectl apply -f k8s/`", "rolled out with Helm (`helm upgrade orders ./chart`)")
    (root / "README.md").write_text(text)
    result = svc.refresh_repository(name)
    assert result["files_modified"] == 1 and result["files_added"] == 0
    assert result["files_unchanged"] == result["files_discovered"] - 1
    item = svc.get_knowledge(dep["id"], repository=name)["item"]
    assert "helm upgrade" in item["content"]           # same id, updated in place
    assert item["status"] == "active"
    # Unchanged sections keep their verification revision; no stale copies of the old text remain active.
    rows = svc.conn.execute("SELECT content FROM knowledge WHERE repo_id=? AND status='active' AND content LIKE '%kubectl apply -f k8s/%' AND origin='extracted' AND content LIKE '%laptop%'", (repo.id,)).fetchall()
    assert rows == []


def test_removed_section_is_invalidated_not_returned(indexed):
    svc, root, name = indexed
    hit = svc.search_knowledge("checkout hangs webhook secret", repository=name)["results"][0]
    assert "Troubleshooting" in hit["title"]
    text = (root / "README.md").read_text()
    (root / "README.md").write_text(text[: text.index("## Troubleshooting")])
    result = svc.refresh_repository(name)
    assert result["knowledge_invalidated"] >= 1
    item = svc.get_knowledge(hit["id"], repository=name)["item"]
    assert item["status"] == "invalidated" and "no longer present" in item["status_reason"]
    ids = [r["id"] for r in svc.search_knowledge("checkout hangs webhook secret", repository=name)["results"]]
    assert hit["id"] not in ids


def test_deleted_file_invalidates_knowledge(indexed):
    svc, root, name = indexed
    adr = svc.search_knowledge("PostgreSQL order storage decision", repository=name, categories=["decision"])["results"][0]
    (root / "docs/adr/0001-use-postgres.md").unlink()
    result = svc.refresh_repository(name)
    assert result["files_deleted"] == 1
    assert svc.get_knowledge(adr["id"], repository=name)["item"]["status"] == "invalidated"
    assert svc.conn.execute("SELECT COUNT(*) FROM files WHERE path='docs/adr/0001-use-postgres.md'").fetchone()[0] == 0
    assert svc.conn.execute("SELECT COUNT(*) FROM symbols WHERE path='docs/adr/0001-use-postgres.md'").fetchone()[0] == 0


def test_rename_keeps_agent_knowledge_and_doc_ids(indexed):
    svc, root, name = indexed
    rec = svc.record_knowledge("implementation", "Discounts above 100 are applied before tax in total_price.", repository=name,
                               sources=[{"path": "src/orders/pricing.py", "start_line": 6, "end_line": 8}])
    arch = svc.search_knowledge("data flow celery confirmation email", repository=name)["results"][0]
    git(root, "mv", "src/orders/pricing.py", "src/orders/prices.py")
    git(root, "mv", "docs/architecture.md", "docs/overview.md")
    result = svc.refresh_repository(name)
    assert result["files_renamed"] == 2
    item = svc.get_knowledge(rec["id"], repository=name)["item"]
    assert item["status"] == "active"
    assert item["sources"][0]["path"] == "src/orders/prices.py"
    moved = svc.get_knowledge(arch["id"], repository=name)["item"]
    assert moved["status"] == "active" and moved["sources"][0]["path"] == "docs/overview.md"


def test_agent_knowledge_goes_stale_when_source_changes(indexed):
    svc, root, name = indexed
    rec = svc.record_knowledge("debugging", "OrderRepository.save is a no-op stub; orders are not persisted yet.", repository=name,
                               sources=[{"path": "src/orders/db.py"}])
    write(root, "src/orders/db.py", '"""Repository layer."""\n\nclass OrderRepository:\n    def save(self, order):\n        return order\n')
    result = svc.refresh_repository(name)
    assert result["agent_items_marked_stale"] == 1
    item = svc.get_knowledge(rec["id"], repository=name)["item"]
    assert item["status"] == "stale" and "changed" in item["status_reason"]
    found = svc.search_knowledge("OrderRepository save persisted", repository=name)
    hit = next(r for r in found["results"] if r["id"] == rec["id"])
    assert hit["status"] == "stale"
    assert hit["sources"][0]["evidence"] == "source changed since recorded"
    # Re-verifying against the new source makes it active again.
    svc.update_knowledge_status(rec["id"], "verify", reason="checked", repository=name)
    assert svc.get_knowledge(rec["id"], repository=name)["item"]["status"] == "active"


def test_secrets_and_sensitive_files_never_stored(indexed):
    svc, root, name = indexed
    db_dump = "\n".join(" ".join(str(v) for v in tuple(r)) for t in ("knowledge", "symbols", "files", "knowledge_sources")
                        for r in svc.conn.execute(f"SELECT * FROM {t}"))
    assert AWS_KEY not in db_dump
    assert "hunter2secret" not in db_dump
    assert STRIPE_SECRET not in db_dump
    assert "super-secret-value" not in db_dump
    paths = {r["path"] for r in svc.conn.execute("SELECT path FROM files")}
    assert ".env" not in paths and "certs/server.pem" not in paths and "config/secrets.yaml" not in paths
    src = svc.get_source("src/orders/db.py", repository=name)
    assert AWS_KEY not in src["content"] and "[REDACTED]" in src["content"] and src["redactions"] >= 2


def test_private_key_in_ordinary_file_is_skipped(make_repo, service):
    root = make_repo({"README.md": "# x\n\nA repository with a key pasted into a text file by mistake.\n",
                      "notes/key.txt": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"})
    rid = service.register(str(root))["id"]
    result = service.refresh_repository(rid)
    assert result["files_skipped_content"] == 1
    row = service.conn.execute("SELECT status, skip_reason FROM files WHERE path='notes/key.txt'").fetchone()
    assert row["status"] == "skipped" and "private key" in row["skip_reason"]


def test_interrupted_run_recovers(make_repo, service, monkeypatch):
    root = make_repo()
    rid = service.register(str(root))["id"]
    calls = {"n": 0}
    real = indexer.Indexer._process

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 5:
            raise KeyboardInterrupt
        return real(self, *a, **kw)

    monkeypatch.setattr(indexer.Indexer, "_process", flaky)
    with pytest.raises(KeyboardInterrupt):
        service.refresh_repository(rid)
    run = service.conn.execute("SELECT status FROM index_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "interrupted"
    assert service.resolve(rid).indexed_revision is None          # never claimed complete
    assert service.repository_status(rid)["freshness"]["state"] in ("never_indexed", "incomplete")
    monkeypatch.setattr(indexer.Indexer, "_process", real)
    result = service.refresh_repository(rid)
    assert result["status"] == "completed"
    assert result["files_unchanged"] == 4                          # the four files done before the interruption
    assert service.resolve(rid).indexed_revision is not None


def test_crashed_process_run_is_taken_over(indexed):
    svc, root, name = indexed
    repo = svc.resolve(name)
    svc.conn.execute(
        "INSERT INTO index_runs(repo_id, mode, status, pid, started_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
        (repo.id, "incremental", "running", 999_999_9, "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"),
    )
    assert svc.refresh_repository(name)["status"] == "completed"
    statuses = [r["status"] for r in svc.conn.execute("SELECT status FROM index_runs WHERE repo_id=? ORDER BY id", (repo.id,))]
    assert "interrupted" in statuses


def test_concurrent_run_is_refused(indexed):
    svc, root, name = indexed
    repo = svc.resolve(name)
    svc.conn.execute(
        "INSERT INTO index_runs(repo_id, mode, status, pid, started_at, heartbeat_at) VALUES (?,?,?,?,?,datetime('now'))",
        (repo.id, "incremental", "running", os.getpid(), "2020-01-01T00:00:00Z"),
    )
    svc.conn.execute("UPDATE index_runs SET heartbeat_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE status='running'")
    assert svc.refresh_repository(name)["status"] == "busy"


def test_extractor_error_is_contained(indexed, monkeypatch):
    svc, root, name = indexed
    write(root, "src/orders/extra.py", "def helper():\n    return 1\n")
    monkeypatch.setattr(code_extractor, "extract", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    result = svc.refresh_repository(name)
    assert result["status"] == "completed" and result["files_error"] == 1
    monkeypatch.undo()
    result = svc.refresh_repository(name)
    assert result["files_error"] == 0 and result["files_added"] == 0 and result["files_modified"] == 0
    assert svc.conn.execute("SELECT status FROM files WHERE path='src/orders/extra.py'").fetchone()["status"] == "indexed"


def test_full_rebuild_is_idempotent(indexed):
    svc, root, name = indexed
    before = {r["id"] for r in svc.conn.execute("SELECT id FROM knowledge WHERE status='active'")}
    result = svc.refresh_repository(name, full=True)
    after = {r["id"] for r in svc.conn.execute("SELECT id FROM knowledge WHERE status='active'")}
    assert before == after
    assert result["knowledge_created"] == 0


def test_fix_commits_become_historical_debugging_knowledge(indexed):
    svc, root, name = indexed
    write(root, "src/orders/pricing.py", (root / "src/orders/pricing.py").read_text().replace("> 100", ">= 100"))
    git(root, "commit", "-qam", "Fix off-by-one in discount threshold")
    result = svc.refresh_repository(name)
    assert result["commits_ingested"] == 1
    hits = svc.search_knowledge("discount threshold off-by-one", repository=name)["results"]
    commit_item = next(h for h in hits if h["provenance"] == "git/historical")
    assert "src/orders/pricing.py" in commit_item["content"]
    assert json.dumps(commit_item["sources"]).count("commit") >= 1
