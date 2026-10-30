import json
import os
import sys

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from knock.mcp_server import build_server
from knock.service import KnockService

READ_ONLY = {"get_context", "search_knowledge", "get_knowledge", "get_source", "get_repository_status"}
MUTATING = {"record_knowledge", "update_knowledge_status", "refresh_repository"}


def run(coro_fn):
    return anyio.run(coro_fn)


@pytest.fixture
def server(indexed, config):
    svc, root, name = indexed
    rid = svc.resolve(name).id
    scoped = KnockService(config, allowed_repo_ids=[rid])
    yield build_server(scoped), root
    scoped.close()


def test_tools_are_listed_with_annotations(server):
    srv, _ = server

    async def go():
        async with Client(srv) as c:
            init = c.initialize_result if hasattr(c, "initialize_result") else None
            tools = {t.name: t for t in (await c.list_tools()).tools}
            return tools, init

    tools, _ = run(go)
    assert set(tools) == READ_ONLY | MUTATING
    for name in READ_ONLY:
        assert tools[name].annotations.read_only_hint is True
    for name in MUTATING:
        assert tools[name].annotations.read_only_hint is False
    assert "category" in tools["record_knowledge"].input_schema["properties"]
    assert len(tools["get_context"].description) > 80


def test_end_to_end_agent_workflow(server):
    srv, root = server

    async def go():
        async with Client(srv) as c:
            ctx = await c.call_tool("get_context", {"task": "Why do we use PostgreSQL for orders?", "max_chars": 4000})
            assert not ctx.is_error
            body = ctx.structured_content
            assert json.loads(ctx.content[0].text) == body               # compact JSON mirrors structured content
            decision = next(k for k in body["knowledge"] if k["category"] == "decision")
            item = await c.call_tool("get_knowledge", {"knowledge_id": decision["id"], "include_evidence": True})
            evidence = item.structured_content["item"]["evidence"][0]
            assert evidence["path"] == "docs/adr/0001-use-postgres.md"
            src = await c.call_tool("get_source", {"path": "src/orders/pricing.py", "symbol": "total_price"})
            assert "def total_price" in src.structured_content["content"]
            rec = await c.call_tool("record_knowledge", {
                "category": "debugging",
                "content": "total_price rounds after tax, so line items can sum to one cent less than the order total.",
                "sources": [{"path": "src/orders/pricing.py", "start_line": 6, "end_line": 8}],
            })
            assert rec.structured_content["outcome"] == "created"
            found = await c.call_tool("search_knowledge", {"query": "rounding cent order total", "categories": ["debugging"]})
            assert found.structured_content["results"][0]["id"] == rec.structured_content["id"]
            upd = await c.call_tool("update_knowledge_status", {"knowledge_id": rec.structured_content["id"], "action": "verify", "reason": "checked"})
            assert upd.structured_content["status"] == "active"
            status = await c.call_tool("get_repository_status", {})
            assert status.structured_content["freshness"]["state"] == "fresh"
            refresh = await c.call_tool("refresh_repository", {})
            assert refresh.structured_content["status"] == "completed" and refresh.structured_content["model_calls"] == 0

    run(go)


def test_errors_are_clean(server):
    srv, _ = server

    async def go():
        async with Client(srv) as c:
            bad_path = await c.call_tool("get_source", {"path": "../../etc/passwd"})
            bad_cat = await c.call_tool("record_knowledge", {"category": "gossip", "content": "something long enough"})
            missing = await c.call_tool("get_knowledge", {"knowledge_id": "k_000000000000"})
            other_repo = await c.call_tool("search_knowledge", {"query": "x", "repository": "not-served"})
            return bad_path, bad_cat, missing, other_repo

    for result in run(go):
        assert result.is_error
        assert "Traceback" not in result.content[0].text
    bad_path = run(go)[0]
    assert ".." in bad_path.content[0].text


def test_stdio_server_subprocess(indexed, config):
    """Launch `python -m knock serve` exactly as an MCP client would and talk to it over stdio."""
    svc, root, name = indexed
    env = {**os.environ, "KNOCK_HOME": str(config.home), "KNOCK_LOG_LEVEL": "WARNING"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "knock", "serve", "--repo", str(root), "--no-refresh"], env=env)

    async def go():
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            res = await c.call_tool("search_knowledge", {"query": "deploy production"})
            return names, res

    names, res = run(go)
    assert names == READ_ONLY | MUTATING
    assert not res.is_error and res.structured_content["results"]
