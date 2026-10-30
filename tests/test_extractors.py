import textwrap

from knock.extractors import extract_file


def ex(path, text, language, kind):
    return extract_file(path, textwrap.dedent(text).lstrip("\n"), language, kind)


def by_key(result):
    return {c.key: c for c in result.candidates}


def test_python_symbols_routes_and_summary():
    r = ex("src/app.py", '''
        """Order API."""
        from fastapi import FastAPI
        app = FastAPI()
        MAX_ITEMS = 10

        class Service(Base):
            def run(self, x):
                pass

        @app.get("/orders/{id}")
        async def get_order(id: int):
            pass

        if __name__ == "__main__":
            main()
        ''', "python", "code")
    names = {(s.kind, s.name) for s in r.symbols}
    assert ("class", "Service") in names
    assert ("method", "Service.run") in names
    assert ("function", "get_order") in names
    assert ("route", "GET /orders/{id}") in names
    assert ("entrypoint", "__main__") in names
    summary = by_key(r)["file-summary"]
    assert summary.category == "implementation"
    assert "Order API." in summary.content and "GET /orders/{id}" in summary.content


def test_python_syntax_error_falls_back():
    r = ex("bad.py", "def broken(:\n    pass\n", "python", "code")
    assert r is not None  # no exception


def test_typescript_and_go_symbols():
    ts = ex("web/a.ts", """
        export interface User { id: string }
        export const load = async (id: string) => fetch(id)
        export default class Store {}
        router.get('/users/:id', handler)
        """, "typescript", "code")
    names = {s.name for s in ts.symbols}
    assert {"User", "load", "Store", "GET /users/:id"} <= names
    go = ex("cmd/server/main.go", """
        package main

        type Server struct{}

        func (s *Server) Start() error { return nil }

        func main() {}
        """, "go", "code")
    names = {s.name for s in go.symbols}
    assert {"Server", "Server.Start", "main"} <= names


def test_markdown_sections_categories_and_commands():
    r = ex("README.md", """
        # Thing

        Thing does stuff for people who need stuff done in a reliable way.

        ## Deployment

        Deploys happen from CI on tags, using the container image built by the pipeline.

        ```bash
        $ make release
        ```

        ## Troubleshooting

        When the worker hangs, the queue credentials are missing from the environment variables.

        ## Why Postgres

        We chose Postgres because we need transactions across orders and payments.
        """, "markdown", "doc")
    cands = {c.title: c for c in r.candidates}
    assert cands["Thing › Deployment"].category == "workflow"
    assert cands["Thing › Deployment"].content.startswith("Commands: `make release`")
    assert "deployment" in cands["Thing › Deployment"].entities
    assert cands["Thing › Troubleshooting"].category == "debugging"
    assert cands["Thing › Why Postgres"].category == "decision"
    dep = cands["Thing › Deployment"]
    assert dep.start_line == 5 and dep.end_line >= 9


def test_rst_sections():
    r = ex("docs/deploying.rst", """
        Deploying to Production
        =======================

        Do not use the development server in production; use a WSGI server instead.

        Gunicorn
        --------

        Gunicorn is a pure Python WSGI server with simple configuration.

        .. code-block:: text

            $ gunicorn -w 4 'app:create_app()'
        """, "rst", "doc")
    titles = {c.title: c for c in r.candidates}
    g = titles["Deploying to Production › Gunicorn"]
    assert g.category == "workflow"
    assert "gunicorn -w 4" in g.content


def test_adr_becomes_decision_with_rationale():
    r = ex("docs/adr/0002-queue.md", """
        # 2. Use Celery

        ## Status

        Superseded by ADR 5

        ## Context

        We need background email sending.

        ## Decision

        Use Celery with Redis as the broker.

        ## Consequences

        Redis becomes a production dependency.
        """, "markdown", "doc")
    (adr,) = r.candidates
    assert adr.category == "decision"
    assert "Celery with Redis" in adr.content
    assert "background email" in adr.rationale and "Redis becomes" in adr.rationale
    assert adr.status == "superseded"


def test_manifests():
    pkg = by_key(ex("package.json", '{"name":"w","scripts":{"test":"vitest"},"dependencies":{"stripe":"1","react":"18"},"devDependencies":{"vitest":"1"}}', "json", "config"))
    assert "`npm run test` → `vitest`" in pkg["pkg:scripts"].content
    assert "Stripe (payments)" in pkg["deps:integrations"].content
    assert "vitest" in pkg["deps:dev"].content
    py = by_key(ex("pyproject.toml", """
        [project]
        name = "x"
        dependencies = ["fastapi>=0.1", "redis[hiredis]"]
        [tool.ruff]
        line-length = 100
        """, "toml", "config"))
    assert "fastapi" in py["deps:runtime"].content and "Redis" in py["deps:integrations"].content
    assert "line-length=100" in py["py:ruff"].content
    gha = ex(".github/workflows/release.yml", """
        name: Release
        on:
          push:
            tags: ["v*"]
        jobs:
          publish:
            runs-on: ubuntu-latest
            steps:
              - run: python -m build
        """, "yaml", "config").candidates[0]
    assert gha.category == "workflow" and "deployment" in gha.entities and "push" in gha.content
    mk = ex("Makefile", "test:\n\tpytest -q\n.PHONY: test\n", "make", "config").candidates[0]
    assert "`make test` → `pytest -q`" in mk.content
    dock = ex("Dockerfile", "FROM node:20\nEXPOSE 3000\nCMD [\"node\", \"server.js\"]\n", "dockerfile", "config").candidates[0]
    assert "node:20" in dock.content and "3000" in dock.content


def test_broken_manifest_does_not_raise():
    assert ex("package.json", "{not json", "json", "config").candidates == []
