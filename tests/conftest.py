from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from knock import indexer
from knock.config import Config
from knock.service import KnockService

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
STRIPE_SECRET = "sk_live_51HxYzAbCdEfGhIjKlMnOp"


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env).stdout


def write(root: Path, rel: str, content: str | bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(textwrap.dedent(content).lstrip("\n"))
    return p


SAMPLE = {
    "README.md": """
        # Orders Service

        Orders Service accepts customer orders over HTTP and stores them in PostgreSQL. Payment capture is
        delegated to Stripe and confirmation emails are sent asynchronously by a Celery worker.

        ## Installation

        Install the Python dependencies and the web client:

        ```bash
        pip install -e .
        npm install
        ```

        ## Running tests

        Tests use pytest and need no external services.

        ```bash
        make test
        ```

        ## Deployment

        Production deploys run from GitHub Actions on every tag. The container image is pushed to the registry
        and rolled out with `kubectl apply -f k8s/`. Never deploy from a laptop.

        ## Troubleshooting

        If checkout requests hang, the Stripe webhook secret is probably missing from the environment; the
        worker retries forever without it. Set STRIPE_WEBHOOK_SECRET and restart the worker.
        """,
    "docs/architecture.md": """
        # Architecture

        ## Components

        The HTTP API lives in `src/orders/api.py`. Business rules for pricing live in `src/orders/pricing.py` and
        never import the web framework. Persistence goes through the repository layer in `src/orders/db.py`.

        ## Data flow

        A request is validated by the API layer, priced by the pricing module, persisted, and then a Celery task
        sends the confirmation email.
        """,
    "docs/adr/0001-use-postgres.md": """
        # 1. Use PostgreSQL for order storage

        ## Status

        Accepted

        ## Context

        Orders need transactional integrity across line items and payments. The team already operates PostgreSQL.

        ## Decision

        We will store orders in PostgreSQL and access it through SQLAlchemy.

        ## Consequences

        We need migrations (Alembic) and a local database for integration tests.
        """,
    "src/orders/__init__.py": '"""Orders service package."""\n',
    "src/orders/api.py": '''
        """HTTP API for creating and listing orders."""

        from fastapi import FastAPI

        from .pricing import total_price

        app = FastAPI()


        @app.post("/orders")
        def create_order(payload: dict) -> dict:
            """Create an order and return its id."""
            return {"total": total_price(payload["items"])}


        @app.get("/orders/{order_id}")
        def get_order(order_id: int) -> dict:
            return {"id": order_id}
        ''',
    "src/orders/pricing.py": '''
        """Pricing rules: discounts and tax. Pure functions, no I/O."""

        TAX_RATE = 0.2


        def total_price(items: list[dict]) -> float:
            subtotal = sum(i["price"] * i["qty"] for i in items)
            return round(apply_discount(subtotal) * (1 + TAX_RATE), 2)


        def apply_discount(subtotal: float) -> float:
            return subtotal * 0.9 if subtotal > 100 else subtotal
        ''',
    "src/orders/db.py": f'''
        """Repository layer over SQLAlchemy."""

        DATABASE_URL = "postgresql://orders:hunter2secret@localhost/orders"
        AWS_ACCESS_KEY_ID = "{AWS_KEY}"


        class OrderRepository:
            def save(self, order):
                pass

            def find(self, order_id):
                pass
        ''',
    "web/client.ts": """
        // Browser client for the orders API.
        export interface Order { id: number; total: number }

        export async function fetchOrder(id: number): Promise<Order> {
          const res = await fetch(`/orders/${id}`);
          return res.json();
        }

        export class OrdersClient {
          constructor(private base: string) {}
        }
        """,
    "tests/test_pricing.py": """
        from orders.pricing import total_price


        def test_total_price_applies_tax():
            assert total_price([{"price": 10, "qty": 1}]) == 12.0
        """,
    "package.json": """
        {
          "name": "orders-web",
          "scripts": {"build": "tsc -p .", "test": "vitest run", "lint": "eslint web"},
          "dependencies": {"stripe": "^14.0.0"},
          "devDependencies": {"typescript": "^5.4.0", "vitest": "^1.0.0"}
        }
        """,
    "pyproject.toml": """
        [project]
        name = "orders"
        description = "Order intake service"
        requires-python = ">=3.10"
        dependencies = ["fastapi>=0.110", "sqlalchemy>=2", "celery>=5", "psycopg[binary]>=3", "stripe>=8"]

        [project.optional-dependencies]
        dev = ["pytest>=8", "ruff"]

        [tool.ruff]
        line-length = 110

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """,
    "Makefile": """
        test:
        \tpytest -q

        lint:
        \truff check .
        """,
    "Dockerfile": """
        FROM python:3.12-slim
        WORKDIR /app
        COPY . .
        EXPOSE 8000
        CMD ["uvicorn", "orders.api:app", "--host", "0.0.0.0"]
        """,
    ".github/workflows/deploy.yml": """
        name: Deploy
        on:
          push:
            tags: ["v*"]
        jobs:
          deploy:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - run: docker build -t orders .
              - run: kubectl apply -f k8s/
        """,
    ".editorconfig": """
        root = true

        [*.py]
        indent_style = space
        indent_size = 4
        """,
    ".gitignore": "build/\n*.log\n",
    ".env": f"STRIPE_SECRET_KEY={STRIPE_SECRET}\n",
    "config/secrets.yaml": "db_password: super-secret-value\n",
    "build/bundle.js": "console.log('generated')\n",
    "debug.log": "noise\n",
    "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00binary",
    "certs/server.pem": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----\n",
}


@pytest.fixture
def config(tmp_path: Path) -> Config:
    home = tmp_path / "knock-home"
    indexer.reset_freshness_cache()
    return Config(home=home, freshness_ttl_seconds=0.0)


@pytest.fixture
def make_repo(tmp_path: Path):
    counter = {"n": 0}

    def _make(files: dict[str, str | bytes] | None = None, name: str | None = None, commit: bool = True) -> Path:
        counter["n"] += 1
        root = tmp_path / (name or f"repo{counter['n']}")
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        for rel, content in (SAMPLE if files is None else files).items():
            write(root, rel, content)
        if commit:
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "Initial commit")
        return root

    return _make


@pytest.fixture
def service(config: Config):
    svc = KnockService(config)
    yield svc
    svc.close()


@pytest.fixture
def indexed(service: KnockService, make_repo):
    """A registered and indexed sample repository: returns (service, repo_root, repo_name)."""
    root = make_repo()
    info = service.register(str(root))
    result = service.refresh_repository(info["id"])
    assert result["status"] == "completed"
    return service, root, info["name"]
