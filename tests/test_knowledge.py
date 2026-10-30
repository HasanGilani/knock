import pytest

from knock.service import KnockError


def test_record_and_retrieve_with_provenance(indexed):
    svc, root, name = indexed
    rec = svc.record_knowledge(
        "debugging",
        "Checkout hangs when STRIPE_WEBHOOK_SECRET is unset because the worker retries forever; set it and restart the worker.",
        repository=name, title="Checkout hang root cause", rationale="Reproduced locally by unsetting the variable.",
        sources=[{"path": "README.md", "start_line": 30, "end_line": 34}], entities=["STRIPE_WEBHOOK_SECRET"],
    )
    assert rec["outcome"] == "created" and rec["verification"] == "unverified"
    item = svc.get_knowledge(rec["id"], repository=name, include_evidence=True)["item"]
    assert item["origin"] == "agent" and item["verification"] == "unverified" and item["status"] == "active"
    assert item["sources"][0]["path"] == "README.md" and item["sources"][0]["lines"] == "30-34"
    assert item["evidence"][0]["path"] == "README.md" and "\t" in item["evidence"][0]["content"]
    assert item["verified_revision"] == svc.resolve(name).indexed_revision


def test_exact_and_near_duplicates_are_merged(indexed):
    svc, root, name = indexed
    first = svc.record_knowledge("convention", "Pricing functions in src/orders/pricing.py must stay pure: no I/O and no framework imports.", repository=name)
    again = svc.record_knowledge("convention", "Pricing functions in src/orders/pricing.py must stay pure: no I/O and no framework imports!", repository=name,
                                 sources=[{"path": "src/orders/pricing.py"}])
    assert again["outcome"] == "deduplicated" and again["duplicate_of"] == first["id"]
    near = svc.record_knowledge("convention", "Pricing functions in src/orders/pricing.py must stay pure, with no I/O and no web framework imports.", repository=name)
    assert near["outcome"] == "deduplicated" and near["similarity"] >= 0.8
    item = svc.get_knowledge(first["id"], repository=name)["item"]
    assert item["observations"] == 3
    assert any(s["path"] == "src/orders/pricing.py" for s in item["sources"])


def test_conflicts_are_reported_and_supersede_works(indexed):
    svc, root, name = indexed
    old = svc.record_knowledge("workflow", "Integration tests require a local PostgreSQL 14 database on port 5432.", repository=name)
    new = svc.record_knowledge("workflow", "Integration tests require a local PostgreSQL 16 database on port 5433.", repository=name)
    assert new["outcome"] == "created"
    assert [c["id"] for c in new["possible_conflicts"]] == [old["id"]]
    hits = svc.search_knowledge("integration tests local PostgreSQL port", repository=name)["results"]
    flagged = {h["id"]: h.get("conflicts_with") for h in hits}
    assert flagged[new["id"]] == [old["id"]]
    newest = svc.record_knowledge("workflow", "Integration tests now use the dockerised PostgreSQL 16 from docker compose on port 5433.",
                                  repository=name, supersedes=[old["id"], new["id"]])
    assert set(newest["superseded"]) == {old["id"], new["id"]}
    assert svc.get_knowledge(old["id"], repository=name)["item"]["status"] == "superseded"
    ids = {h["id"] for h in svc.search_knowledge("integration tests PostgreSQL port", repository=name)["results"]}
    assert newest["id"] in ids and old["id"] not in ids
    history = {h["id"] for h in svc.search_knowledge("integration tests PostgreSQL port", repository=name, include_inactive=True)["results"]}
    assert old["id"] in history


def test_invalidate_hides_item(indexed):
    svc, root, name = indexed
    rec = svc.record_knowledge("implementation", "Order totals are computed in web/client.ts on the client side.", repository=name)
    svc.update_knowledge_status(rec["id"], "invalidate", reason="wrong: totals are computed in pricing.py", repository=name)
    item = svc.get_knowledge(rec["id"], repository=name)["item"]
    assert item["status"] == "invalidated" and "pricing.py" in item["status_reason"]
    assert rec["id"] not in {h["id"] for h in svc.search_knowledge("order totals computed client", repository=name)["results"]}


def test_extracted_knowledge_cannot_be_verified_by_agent(indexed):
    svc, root, name = indexed
    hit = svc.search_knowledge("deployment", repository=name)["results"][0]
    with pytest.raises(KnockError):
        svc.update_knowledge_status(hit["id"], "verify", repository=name)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"category": "gossip", "content": "something long enough"}, "category"),
        ({"category": "debugging", "content": "short"}, "at least 10"),
        ({"category": "debugging", "content": "x" * 5000}, "exceeds"),
        ({"category": "debugging", "content": "valid content here", "sources": [{"path": "../outside.py"}]}, "invalid source path"),
        ({"category": "debugging", "content": "valid content here", "sources": [{"path": ".env"}]}, "not an indexed file"),
        ({"category": "debugging", "content": "valid content here", "sources": [{"path": "README.md", "start_line": 9, "end_line": 2}]}, "end_line"),
        ({"category": "debugging", "content": "valid content here", "supersedes": ["k_doesnotexist"]}, "unknown knowledge id"),
    ],
)
def test_record_validation(indexed, kwargs, message):
    svc, root, name = indexed
    with pytest.raises(KnockError, match=message):
        svc.record_knowledge(repository=name, **kwargs)


def test_recorded_secrets_are_redacted(indexed):
    svc, root, name = indexed
    rec = svc.record_knowledge("debugging", 'Local login works with api_key = "sk_live_abcdefghijklmnop1234"', repository=name)
    assert rec["redactions"] == 1
    assert "sk_live_" not in svc.get_knowledge(rec["id"], repository=name)["item"]["content"]
