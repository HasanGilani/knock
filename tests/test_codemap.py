"""knock v2: structural code map, exact-code open, lenient remember, and the 3-tool MCP surface."""

import anyio
import pytest
from mcp import Client

from knock.codemap import code_identifiers
from knock.mcp_server import build_server
from knock.service import KnockError, KnockService

from .conftest import git, write


def test_code_identifiers():
    ids = code_identifiers("Users are logged out when SECRET_KEY_FALLBACKS is set; see open_session and App.secret_key in FlaskClient")
    assert {"SECRET_KEY_FALLBACKS", "open_session", "App.secret_key", "FlaskClient"} <= set(ids)
    assert "Users" not in ids and "logged" not in ids


def test_map_shows_structure_for_a_task(indexed):
    svc, root, name = indexed
    text = svc.code_map("Change how the discount is applied when computing the order total price", repository=name)
    assert text.startswith(f"knock map · {name} @")
    assert "src/orders/pricing.py" in text
    assert "total_price(items)" in text and "L6-" in text            # signature + exact line range
    assert "apply_discount(subtotal)" in text
    assert "called by" in text                                         # call graph
    assert "subtotal * 0.9" not in text                                # no function bodies
    assert len(text) <= 3000


def test_map_finds_code_by_identifier_in_task(indexed):
    svc, root, name = indexed
    text = svc.code_map("Where is TAX_RATE used?", repository=name)
    assert "total_price" in text


def test_map_includes_why_from_commits(indexed):
    svc, root, name = indexed
    write(root, "src/orders/pricing.py", (root / "src/orders/pricing.py").read_text().replace("> 100", ">= 100"))
    git(root, "commit", "-qam", "Fix discount threshold\n\nOrders of exactly 100 were not discounted because the comparison was strict.")
    svc.refresh_repository(name)
    text = svc.code_map("why are orders of exactly 100 discounted?", repository=name)
    assert "Why / history:" in text and "comparison was strict" in text


def test_map_reindexes_changed_files_itself(indexed):
    svc, root, name = indexed
    src = root / "src/orders/pricing.py"
    src.write_text(src.read_text() + "\n\ndef loyalty_bonus(points):\n    return points * 0.01\n")
    text = svc.code_map("how is the loyalty bonus computed from points?", repository=name)
    assert "re-indexed 1 changed file" in text
    assert "loyalty_bonus(points)" in text


def test_open_symbol_range_and_skeleton(indexed):
    svc, root, name = indexed
    out = svc.open_code("total_price", repository=name)
    assert out.startswith("total_price  src/orders/pricing.py")
    assert "def total_price" in out and "\t" in out                    # exact, line-numbered code
    assert "Calls:" in out and "apply_discount" in out
    out = svc.open_code("src/orders/pricing.py:1-2", repository=name)
    assert out.startswith("src/orders/pricing.py  L1-2")
    out = svc.open_code("src/orders/pricing.py", repository=name)
    assert "skeleton" in out and "def total_price(items)" in out and "return" not in out
    with pytest.raises(KnockError):
        svc.open_code("no_such_function_anywhere", repository=name)
    with pytest.raises(KnockError):
        svc.open_code("../../etc/passwd:1-2", repository=name)


def test_open_lists_choices_for_ambiguous_names(make_repo, service):
    root = make_repo({"README.md": "# Two\n\nTwo modules that both define a function called load.\n",
                      "a.py": "def load(x):\n    return x\n", "b.py": "def load(y):\n    return y\n"})
    rid = service.register(str(root))["id"]
    service.refresh_repository(rid)
    out = service.open_code("load", repository=rid)
    assert "2 symbols match" in out and "a.py:1" in out and "b.py:1" in out


def test_module_level_objects_are_symbols(indexed):
    svc, root, name = indexed
    text = svc.open_code("src/orders/api.py", repository=name)
    assert "app = FastAPI(…)" in text


def test_remember_is_lenient(indexed):
    svc, root, name = indexed
    msg = svc.remember("Root cause of rounding drift: tax is applied after the discount in total_price.",
                       files="src/orders/pricing.py:6-8", repository=name)
    assert msg.startswith("saved k_") and "debugging" in msg       # "root cause" -> debugging
    item = svc.conn.execute("SELECT k.category, ks.path, ks.start_line FROM knowledge k JOIN knowledge_sources ks ON ks.knowledge_id=k.id WHERE k.origin='agent'").fetchone()
    assert item["path"] == "src/orders/pricing.py" and item["start_line"] == 6 and item["category"] == "debugging"
    msg = svc.remember("Pricing never imports the web framework; keep it pure.", files=["src/orders/pricing.py"], repository=name)
    assert "saved" in msg


def test_map_toolset_over_mcp(indexed, config):
    svc, root, name = indexed
    scoped = KnockService(config, allowed_repo_ids=[svc.resolve(name).id])
    server = build_server(scoped, toolset="map")

    async def go():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            m = await c.call_tool("map", {"task": "how is the order total price computed"})
            o = await c.call_tool("open", {"target": "total_price"})
            r = await c.call_tool("remember", {"note": "total_price rounds to 2 decimals after tax.", "files": ["src/orders/pricing.py"]})
            bad = await c.call_tool("open", {"target": "../x:1-2"})
            return tools, m, o, r, bad

    tools, m, o, r, bad = anyio.run(go)
    scoped.close()
    assert set(tools) == {"map", "open", "remember"}
    assert tools["map"].annotations.read_only_hint is True and tools["remember"].annotations.read_only_hint is False
    assert not m.is_error and m.structured_content is None and "total_price" in m.content[0].text
    assert "def total_price" in o.content[0].text
    assert r.content[0].text.startswith("saved k_")
    assert bad.is_error and "Traceback" not in bad.content[0].text


def test_change_request_detection():
    from knock.codemap import is_change_request

    assert is_change_request("Add a debug log when verification fails")
    assert is_change_request("Ops wants visibility: when the job runs, log how many items it queued.")
    assert not is_change_request("Why do users get logged out after we rotate the key?")
    assert not is_change_request("I'm adding a config key. Where do the defaults live?")


def test_edit_mode_map_gives_read_range_and_local_patterns(make_repo, service):
    root = make_repo({
        "README.md": "# Jobs\n\nBackground jobs for the shop.\n",
        "shop/jobs.py": (
            '"""Background jobs."""\n'
            "import logging\n\n"
            "logger = logging.getLogger(__name__)\n\n\n"
            "def purge_carts(carts):\n"
            '    """Delete abandoned carts."""\n'
            "    removed = [c for c in carts if c.abandoned]\n"
            '    logger.info("Purged %d carts.", len(removed))\n'
            "    return removed\n\n\n"
            "def queue_reminders(carts):\n"
            "    for cart in carts:\n"
            "        send_reminder(cart)\n\n\n"
            "def send_reminder(cart):\n"
            "    cart.notify()\n"
        ),
    })
    rid = service.register(str(root))["id"]
    service.refresh_repository(rid)
    text = service.code_map("When the reminder job runs, log how many carts it queued reminders for.", repository=rid)
    assert "· change request" in text.splitlines()[0]
    assert "queue_reminders(carts)" in text
    assert 'shop/jobs.py:10: logger.info("Purged %d carts.", len(removed))' in text   # a real usage, not the import
    assert "import logging" not in text and "Delete abandoned carts" not in text.split("Existing patterns")[1]
    assert "To edit: Read shop/jobs.py with offset" in text
    assert "Docs & config" not in text
    question = service.code_map("How are abandoned carts purged?", repository=rid)
    assert "change request" not in question and "To edit" not in question
