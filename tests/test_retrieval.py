import json

import pytest

from knock.service import KnockError

from .conftest import write


def test_deployment_query_ranks_workflow_first(indexed):
    svc, root, name = indexed
    res = svc.search_knowledge("how do we deploy to production?", repository=name)
    top = res["results"][:3]
    assert top[0]["category"] == "workflow"
    paths = {s["path"] for r in top for s in r["sources"]}
    assert {"README.md", ".github/workflows/deploy.yml"} & paths
    assert res["freshness"]["state"] == "fresh"


def test_decision_query_returns_rationale(indexed):
    svc, root, name = indexed
    res = svc.search_knowledge("why did we choose PostgreSQL?", repository=name)
    top = res["results"][0]
    assert top["category"] == "decision" and "transactional integrity" in top["rationale"]


def test_filters(indexed):
    svc, root, name = indexed
    res = svc.search_knowledge("stripe payments", repository=name, categories=["dependency"])
    assert res["results"] and all(r["category"] == "dependency" for r in res["results"])
    res = svc.search_knowledge("orders", repository=name, path_prefix="src/orders")
    assert res["results"] and all(any(s["path"].startswith("src/orders/") for s in r["sources"]) for r in res["results"])
    with pytest.raises(KnockError):
        svc.search_knowledge("orders", repository=name, categories=["nonsense"])
    with pytest.raises(KnockError):
        svc.search_knowledge("orders", repository=name, path_prefix="../x")


@pytest.mark.parametrize("budget", [600, 1500, 4000])
def test_search_respects_budget(indexed, budget):
    svc, root, name = indexed
    res = svc.search_knowledge("orders service api pricing deploy test", repository=name, max_chars=budget, limit=25)
    assert len(json.dumps(res["results"], ensure_ascii=False, separators=(",", ":"))) <= budget
    assert res["meta"]["response_chars"] <= budget + 800   # freshness/notes/meta overhead is bounded


def test_context_package(indexed):
    svc, root, name = indexed
    ctx = svc.get_context("Change how the discount is applied when computing the order total price", repository=name, max_chars=5000)
    assert ctx["meta"]["response_chars"] <= 5000
    assert any(s["path"] == "src/orders/pricing.py" for s in ctx["symbols"])
    assert ctx["files_to_inspect"][0]["path"] == "src/orders/pricing.py"
    assert any("src/orders/pricing.py" in json.dumps(k["sources"]) for k in ctx["knowledge"])
    assert ctx["freshness"]["state"] == "fresh" and "guidance" in ctx


def test_context_reports_gaps(indexed):
    svc, root, name = indexed
    ctx = svc.get_context("configure the kafka consumer group rebalancing", repository=name)
    assert any("kafka" in g for g in ctx.get("gaps", []))


def test_context_paths_pin_knowledge(indexed):
    svc, root, name = indexed
    ctx = svc.get_context("tidy things up", repository=name, paths=["web/client.ts"])
    assert ctx["files_to_inspect"][0]["path"] == "web/client.ts"
    assert "web/client.ts" in json.dumps(ctx["knowledge"][0]["sources"])


def test_freshness_detects_working_tree_changes_without_reindex(indexed):
    svc, root, name = indexed
    write(root, "src/orders/pricing.py", "def total_price(items):\n    return 0\n")
    res = svc.search_knowledge("pricing total price", repository=name)
    assert res["freshness"]["state"] == "stale"
    assert res["freshness"]["files_modified_since_index"] == 1
    hit = next(r for r in res["results"] if any(s["path"] == "src/orders/pricing.py" for s in r["sources"]))
    assert hit["sources"][0]["evidence"] == "file modified in working tree since last index"
    src = svc.get_source("src/orders/pricing.py", repository=name)
    assert src["changed_since_index"] is True


def test_get_source_bounds_and_symbol(indexed):
    svc, root, name = indexed
    out = svc.get_source("src/orders/pricing.py", repository=name, symbol="apply_discount")
    assert "def apply_discount" in out["content"]
    out = svc.get_source("src/orders/pricing.py", repository=name, start_line=2, end_line=3)
    assert out["start_line"] == 2 and out["end_line"] == 3 and out["content"].startswith("2\t")
    svc.config.source_max_lines = 3
    out = svc.get_source("src/orders/pricing.py", repository=name)
    assert out["end_line"] == 3 and out["truncated"] and out["next_start_line"] == 4


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", ".env", "build/bundle.js", "config/secrets.yaml", ".git/config", "missing.py"])
def test_get_source_refuses_non_indexed_paths(indexed, path):
    svc, root, name = indexed
    with pytest.raises(KnockError):
        svc.get_source(path, repository=name)
