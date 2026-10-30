import socket

import pytest

from knock.config import Config
from knock.service import KnockError, KnockService

from .conftest import write


def test_repeat_query_hits_cache(indexed):
    svc, root, name = indexed
    first = svc.search_knowledge("deploy production", repository=name)
    second = svc.search_knowledge("deploy production", repository=name)
    assert first["meta"]["cached"] is False and second["meta"]["cached"] is True
    assert first["results"] == second["results"]
    other = svc.search_knowledge("deploy production", repository=name, limit=3)
    assert other["meta"]["cached"] is False          # different parameters, different key
    ctx1 = svc.get_context("deploy production", repository=name)
    ctx2 = svc.get_context("deploy production", repository=name)
    assert not ctx1["meta"]["cached"] and ctx2["meta"]["cached"]
    snap = svc.get_metrics(name)
    assert snap["cache"]["hits"] == 2 and snap["cache"]["misses"] == 3


def test_cache_invalidated_by_source_change(indexed):
    svc, root, name = indexed
    q = "kubernetes rollout helm"
    before = svc.search_knowledge(q, repository=name)
    assert not any("helm" in r["content"].lower() for r in before["results"])
    readme = root / "README.md"
    readme.write_text(readme.read_text().replace("`kubectl apply -f k8s/`", "Helm charts (`helm upgrade orders ./chart`)"))
    svc.refresh_repository(name)
    after = svc.search_knowledge(q, repository=name)
    assert after["meta"]["cached"] is False
    assert any("helm" in r["content"].lower() for r in after["results"])


def test_cache_invalidated_by_recorded_knowledge(indexed):
    svc, root, name = indexed
    svc.search_knowledge("flaky celery worker", repository=name)
    svc.record_knowledge("debugging", "The Celery worker is flaky under load because prefetch_multiplier defaults to 4; set it to 1.", repository=name)
    res = svc.search_knowledge("flaky celery worker", repository=name)
    assert res["meta"]["cached"] is False and "prefetch_multiplier" in res["results"][0]["content"]


def test_cache_is_bounded(indexed):
    svc, root, name = indexed
    svc.config.cache_max_entries = 5
    for i in range(12):
        svc.search_knowledge(f"orders query {i}", repository=name)
    assert svc._cache().stats()["entries"] <= 5


def test_repositories_are_isolated(config, make_repo):
    a_root = make_repo(name="alpha")
    b_root = make_repo({"README.md": "# Beta\n\n## Deployment\n\nBeta deploys with Zanzibar rockets from the moon base every Friday.\n",
                        "beta.py": "def zanzibar_launch():\n    pass\n"}, name="beta")
    admin = KnockService(config)
    a = admin.register(str(a_root))
    b = admin.register(str(b_root))
    admin.refresh_repository(a["id"])
    admin.refresh_repository(b["id"])
    beta_item = admin.search_knowledge("zanzibar rockets", repository=b["id"])["results"][0]
    alpha_item = admin.search_knowledge("deploy", repository=a["id"])["results"][0]
    admin.close()

    scoped = KnockService(config, allowed_repo_ids=[a["id"]])
    res = scoped.search_knowledge("zanzibar rockets moon")
    assert all("Zanzibar" not in r["content"] for r in res["results"])
    with pytest.raises(KnockError):
        scoped.search_knowledge("zanzibar", repository=b["id"])          # not served
    with pytest.raises(KnockError):
        scoped.get_knowledge(beta_item["id"])                             # other repo's id
    with pytest.raises(KnockError):
        scoped.get_source("beta.py")                                      # other repo's file
    with pytest.raises(KnockError):
        scoped.record_knowledge("debugging", "valid content goes here", supersedes=[beta_item["id"]])
    with pytest.raises(KnockError):
        scoped.update_knowledge_status(beta_item["id"], "invalidate")
    assert scoped.get_knowledge(alpha_item["id"])["item"]["id"] == alpha_item["id"]
    assert [r["name"] for r in scoped.list_repositories()] == [a["name"]]
    # Identical query against each repository never shares a cache entry.
    admin2 = KnockService(config)
    ra = admin2.search_knowledge("deployment", repository=a["id"])
    rb = admin2.search_knowledge("deployment", repository=b["id"])
    assert not rb["meta"]["cached"]
    assert {r["id"] for r in ra["results"]}.isdisjoint({r["id"] for r in rb["results"]})
    scoped.close()
    admin2.close()


def test_no_network_access_during_index_and_queries(config, make_repo, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("knock attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    root = make_repo()
    svc = KnockService(config)
    rid = svc.register(str(root))["id"]
    svc.refresh_repository(rid)
    svc.get_context("deploy the service", repository=rid)
    svc.search_knowledge("pricing", repository=rid)
    svc.get_source("README.md", repository=rid)
    svc.record_knowledge("workflow", "Deploys require the release tag to start with v.", repository=rid)
    svc.repository_status(rid)
    svc.close()


def test_data_dir_is_private(config, make_repo):
    root = make_repo()
    svc = KnockService(config)
    svc.register(str(root))
    assert oct(config.home.stat().st_mode & 0o777) == "0o700"
    svc.close()


def test_config_rejects_unknown_settings(tmp_path):
    from knock.config import load_config

    (tmp_path / "config.json").write_text('{"telemetry_endpoint": "https://example.com"}')
    with pytest.raises(ValueError):
        load_config(tmp_path)
    (tmp_path / "config.json").write_text('{"exclude": ["fixtures/"], "context_max_chars": 6000}')
    cfg = load_config(tmp_path)
    assert cfg.exclude == ["fixtures/"] and cfg.context_max_chars == 6000
    assert isinstance(Config().max_file_bytes, int)
