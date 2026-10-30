"""Manifest and configuration extraction: dependencies, integrations, workflows, conventions.

Nothing here executes repository content. Manifests are parsed as data (JSON, TOML,
INI) or with conservative line patterns (YAML, Makefile, Dockerfile).
"""

from __future__ import annotations

import configparser
import json
import re
import sys
from pathlib import PurePosixPath

from ..util import clean_text
from . import Candidate

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10
    import tomli as tomllib

MAX_LIST = 40

# Package name fragment -> human-readable integration. Matched against dependency names.
INTEGRATIONS = {
    "stripe": "Stripe (payments)", "boto3": "AWS SDK", "aws-sdk": "AWS SDK", "@aws-sdk": "AWS SDK",
    "google-cloud": "Google Cloud SDK", "@google-cloud": "Google Cloud SDK", "azure": "Azure SDK",
    "redis": "Redis", "ioredis": "Redis", "psycopg": "PostgreSQL", "pg": "PostgreSQL", "asyncpg": "PostgreSQL",
    "mysql": "MySQL", "pymysql": "MySQL", "mongodb": "MongoDB", "pymongo": "MongoDB", "mongoose": "MongoDB",
    "sqlalchemy": "SQLAlchemy ORM", "prisma": "Prisma ORM", "@prisma/client": "Prisma ORM", "typeorm": "TypeORM",
    "sequelize": "Sequelize ORM", "drizzle-orm": "Drizzle ORM", "django": "Django", "flask": "Flask",
    "fastapi": "FastAPI", "express": "Express", "next": "Next.js", "react": "React", "vue": "Vue",
    "svelte": "Svelte", "@angular/core": "Angular", "celery": "Celery (task queue)", "kafka": "Kafka",
    "kafkajs": "Kafka", "pika": "RabbitMQ", "amqplib": "RabbitMQ", "elasticsearch": "Elasticsearch",
    "sentry": "Sentry (error tracking)", "@sentry": "Sentry (error tracking)", "datadog": "Datadog",
    "opentelemetry": "OpenTelemetry", "@opentelemetry": "OpenTelemetry", "openai": "OpenAI API",
    "anthropic": "Anthropic API", "@anthropic-ai/sdk": "Anthropic API", "twilio": "Twilio",
    "sendgrid": "SendGrid", "@sendgrid": "SendGrid", "firebase": "Firebase", "firebase-admin": "Firebase",
    "supabase": "Supabase", "@supabase/supabase-js": "Supabase", "graphql": "GraphQL", "grpc": "gRPC",
    "grpcio": "gRPC", "@grpc/grpc-js": "gRPC", "auth0": "Auth0", "passport": "Passport (auth)",
    "jsonwebtoken": "JWT", "pyjwt": "JWT", "socket.io": "Socket.IO", "websockets": "WebSockets",
    "gunicorn": "Gunicorn (WSGI server)", "uvicorn": "Uvicorn (ASGI server)", "werkzeug": "Werkzeug (WSGI)",
    "jinja2": "Jinja templates", "pydantic": "Pydantic", "zod": "Zod", "tailwindcss": "Tailwind CSS",
}


def extract(path: str, text: str, language: str) -> list[Candidate]:
    p = PurePosixPath(path)
    name = p.name.lower()
    try:
        if name == "package.json":
            return _package_json(path, text)
        if name == "pyproject.toml":
            return _pyproject(path, text)
        if re.match(r"requirements.*\.(txt|in)$", name):
            return _requirements(path, text)
        if name == "go.mod":
            return _go_mod(path, text)
        if name == "cargo.toml":
            return _cargo(path, text)
        if name == "gemfile":
            return _deps(path, "Ruby gems", re.findall(r"^\s*gem\s+['\"]([^'\"]+)['\"]", text, re.M), text)
        if name == "composer.json":
            data = json.loads(text)
            return _deps(path, "PHP packages", list((data.get("require") or {}).keys()), text)
        if name == "pom.xml":
            return _deps(path, "Maven artifacts", re.findall(r"<artifactId>([^<]+)</artifactId>", text)[1:], text)
        if name in ("build.gradle", "build.gradle.kts"):
            found = re.findall(r"(?:implementation|api|compileOnly|runtimeOnly)\s*\(?\s*['\"]([^:'\"]+:[^:'\"]+)", text)
            return _deps(path, "Gradle dependencies", found, text)
        if name in ("makefile", "gnumakefile", "justfile"):
            return _makefile(path, text)
        if language == "dockerfile":
            return _dockerfile(path, text)
        if re.match(r"(docker-)?compose(\.[\w-]+)?\.ya?ml$", name):
            return _compose(path, text)
        if path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
            return _gha(path, text)
        if name == ".gitlab-ci.yml":
            return _gitlab(path, text)
        if name in ("procfile", "fly.toml", "vercel.json", "netlify.toml", "render.yaml", "app.yaml", "railway.json", "heroku.yml", "serverless.yml", "wrangler.toml"):
            return [_simple(path, "workflow", f"Deployment config {path}", f"Deployment configuration for {_deploy_target(name)} in {path}: {_compact_lines(text, 400)}", ["deployment", _deploy_target(name)], text)]
        if name == ".editorconfig":
            return _editorconfig(path, text)
        if name == "tsconfig.json":
            return _tsconfig(path, text)
        if name == "setup.cfg" or name == "tox.ini" or name == ".flake8":
            return _ini_tools(path, text)
        if name == ".pre-commit-config.yaml":
            hooks = re.findall(r"^\s*-\s*id:\s*([\w.-]+)", text, re.M)
            return [_simple(path, "convention", "Pre-commit hooks", f"pre-commit runs these hooks ({path}): {', '.join(hooks[:MAX_LIST])}. Run `pre-commit run --all-files` to check locally.", ["pre-commit", *hooks[:20]], text)] if hooks else []
        if name in (".nvmrc", ".python-version", ".tool-versions", ".node-version", ".ruby-version"):
            return [_simple(path, "workflow", f"Runtime version ({name})", f"Pinned runtime version in {path}: {_compact_lines(text, 160)}", ["runtime-version"], text)]
        if re.match(r"(\.eslintrc(\.\w+)?|eslint\.config\.\w+|\.prettierrc(\.\w+)?|prettier\.config\.\w+|biome\.json|\.rubocop\.yml|\.golangci\.ya?ml|rustfmt\.toml|\.clang-format|\.stylelintrc(\.\w+)?|ruff\.toml|\.ruff\.toml|mypy\.ini|\.pylintrc)$", name):
            tool = name.lstrip(".").split(".")[0]
            tool = tool[:-2] if tool.endswith("rc") else tool
            return [_simple(path, "convention", f"{tool} configuration", f"Code style/lint tool `{tool}` is configured in {path}. Key settings: {_compact_lines(text, 360)}", ["lint", tool], text)]
        if language == "yaml" and re.search(r"^kind:\s*(Deployment|StatefulSet|Service|Ingress|CronJob|DaemonSet)\b", text, re.M):
            kinds = sorted(set(re.findall(r"^kind:\s*(\w+)", text, re.M)))
            names = re.findall(r"^\s{2}name:\s*([\w.-]+)", text, re.M)[:8]
            return [_simple(path, "workflow", f"Kubernetes manifest {path}", f"Kubernetes resources in {path}: {', '.join(kinds)} ({', '.join(names)}).", ["deployment", "kubernetes"], text)]
    except (ValueError, KeyError, TypeError, AttributeError, configparser.Error, tomllib.TOMLDecodeError):
        return []
    return []


# ----------------------------------------------------------------- helpers
def _line_of(text: str, needle: str) -> int | None:
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else None


def _simple(path: str, category: str, title: str, content: str, entities: list[str], text: str, key: str | None = None, start: int | None = None, end: int | None = None, rationale: str | None = None) -> Candidate:
    return Candidate(
        key=key or f"{category}:{title}",
        category=category,
        title=title,
        content=clean_text(content, 900),
        rationale=rationale,
        entities=[e for e in entities if e][:40],
        start_line=start or 1,
        end_line=end or text.count("\n") + 1,
        confidence=0.95,
    )


def _compact_lines(text: str, limit: int) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith(("#", "//"))]
    return clean_text("; ".join(lines), limit)


def _integrations(names: list[str]) -> list[str]:
    found: dict[str, None] = {}
    for n in names:
        low = n.lower()
        for frag, label in INTEGRATIONS.items():
            if low == frag or low.startswith(frag + "-") or low.startswith(frag + "/") or low.startswith(frag + "["):
                found.setdefault(label, None)
    return list(found)


def _dep_name(spec: str) -> str:
    return re.split(r"[<>=!~;\[ @]", spec.strip(), maxsplit=1)[0].strip()


def _deps(path: str, label: str, names: list[str], text: str, dev: list[str] | None = None) -> list[Candidate]:
    names = [n for n in dict.fromkeys(names) if n]
    out: list[Candidate] = []
    if names:
        shown = ", ".join(names[:MAX_LIST]) + (f" (+{len(names) - MAX_LIST} more)" if len(names) > MAX_LIST else "")
        out.append(_simple(path, "dependency", f"{label} ({path})", f"{label} declared in {path}: {shown}.", names[:MAX_LIST], text, key="deps:runtime"))
    if dev:
        dev = list(dict.fromkeys(dev))
        out.append(_simple(path, "dependency", f"Development dependencies ({path})", f"Development/test dependencies in {path}: {', '.join(dev[:MAX_LIST])}.", dev[:MAX_LIST], text, key="deps:dev"))
    integ = _integrations(names)
    if integ:
        out.append(_simple(path, "dependency", f"External integrations ({path})", f"Libraries in {path} indicate these integrations/frameworks: {', '.join(integ)}.", integ, text, key="deps:integrations"))
    return out


# ---------------------------------------------------------------- manifests
def _package_json(path: str, text: str) -> list[Candidate]:
    data = json.loads(text)
    if not isinstance(data, dict):
        return []
    out: list[Candidate] = []
    name = data.get("name") or path
    desc = data.get("description")
    facts = []
    if desc:
        facts.append(str(desc))
    for field_name in ("main", "module", "types"):
        if isinstance(data.get(field_name), str):
            facts.append(f"{field_name}: {data[field_name]}")
    if data.get("bin"):
        bins = data["bin"] if isinstance(data["bin"], dict) else {name: data["bin"]}
        facts.append("CLI binaries: " + ", ".join(f"{k} → {v}" for k, v in list(bins.items())[:8]))
    if data.get("workspaces"):
        ws = data["workspaces"]
        ws = ws.get("packages", []) if isinstance(ws, dict) else ws
        facts.append("Monorepo workspaces: " + ", ".join(map(str, ws[:12])))
    if data.get("type") == "module":
        facts.append("ES modules (\"type\": \"module\")")
    if facts:
        out.append(_simple(path, "architecture", f"Package {name}", f"Node package `{name}` ({path}): " + "; ".join(facts) + ".", [name], text, key="pkg:meta"))
    scripts = data.get("scripts") or {}
    if isinstance(scripts, dict) and scripts:
        items = [f"`npm run {k}` → `{str(v)[:120]}`" for k, v in list(scripts.items())[:25]]
        tags = [t for t in ("build", "test", "lint", "deploy", "start", "dev") if any(t in k for k in scripts)]
        out.append(_simple(path, "workflow", f"npm scripts ({path})", f"Scripts defined in {path}: " + "; ".join(items) + ".", tags + list(scripts)[:20], text, key="pkg:scripts", start=_line_of(text, '"scripts"')))
    engines = data.get("engines")
    if isinstance(engines, dict) and engines:
        out.append(_simple(path, "workflow", f"Runtime engines ({path})", f"Required engines in {path}: " + ", ".join(f"{k} {v}" for k, v in engines.items()) + ".", ["runtime-version"], text, key="pkg:engines"))
    deps = list((data.get("dependencies") or {}).keys()) + list((data.get("peerDependencies") or {}).keys())
    dev = list((data.get("devDependencies") or {}).keys())
    out.extend(_deps(path, "npm dependencies", deps, text, dev))
    return out


def _pyproject(path: str, text: str) -> list[Candidate]:
    data = tomllib.loads(text)
    out: list[Candidate] = []
    project = data.get("project") or {}
    poetry = (data.get("tool") or {}).get("poetry") or {}
    name = project.get("name") or poetry.get("name") or path
    facts = []
    if project.get("description") or poetry.get("description"):
        facts.append(str(project.get("description") or poetry.get("description")))
    if project.get("requires-python"):
        facts.append(f"requires Python {project['requires-python']}")
    backend = (data.get("build-system") or {}).get("build-backend")
    if backend:
        facts.append(f"build backend {backend}")
    scripts = project.get("scripts") or poetry.get("scripts") or {}
    if scripts:
        facts.append("console scripts: " + ", ".join(f"{k} → {v}" for k, v in list(scripts.items())[:10]))
    if facts:
        out.append(_simple(path, "architecture", f"Python project {name}", f"Python project `{name}` ({path}): " + "; ".join(facts) + ".", [name, *scripts.keys()], text, key="py:meta"))
    deps = [_dep_name(d) for d in project.get("dependencies") or []]
    deps += [k for k in (poetry.get("dependencies") or {}) if k.lower() != "python"]
    dev: list[str] = []
    for group, items in (project.get("optional-dependencies") or {}).items():
        dev += [_dep_name(d) for d in items]
    for group, items in (data.get("dependency-groups") or {}).items():
        dev += [_dep_name(d) for d in items if isinstance(d, str)]
    for group in ((poetry.get("group") or {}).values()):
        dev += list((group or {}).get("dependencies", {}).keys())
    out.extend(_deps(path, "Python dependencies", deps, text, dev))
    tool = data.get("tool") or {}
    pytest_cfg = (tool.get("pytest") or {}).get("ini_options")
    if pytest_cfg:
        settings = ", ".join(f"{k}={v}" for k, v in list(pytest_cfg.items())[:8])
        out.append(_simple(path, "workflow", "pytest configuration", f"Tests run with pytest (configured in {path} [tool.pytest.ini_options]: {settings}). Run `pytest`.", ["test", "pytest"], text, key="py:pytest", start=_line_of(text, "[tool.pytest")))
    for linter in ("ruff", "black", "isort", "mypy", "pylint", "flake8", "pyright", "coverage"):
        cfg = tool.get(linter)
        if cfg:
            flat = _flatten(cfg)
            out.append(_simple(path, "convention", f"{linter} configuration", f"`{linter}` is configured in {path}: {clean_text(', '.join(flat), 420)}.", ["lint", linter], text, key=f"py:{linter}", start=_line_of(text, f"[tool.{linter}")))
    tox = tool.get("tox")
    if isinstance(tox, dict) and (tox.get("env") or tox.get("env_run_base")):
        def first_cmd(env: dict) -> str:
            cmds = env.get("commands") or []
            first = cmds[0] if cmds and isinstance(cmds[0], list) else cmds
            return " ".join(x for x in first if isinstance(x, str))[:100]

        parts = []
        base = tox.get("env_run_base") or {}
        if base:
            parts.append(f"default ({base.get('description', 'test run')}): `{first_cmd(base)}`")
        for name, env in list((tox.get("env") or {}).items())[:15]:
            if isinstance(env, dict):
                c = first_cmd(env)
                parts.append(f"{name} ({env.get('description', '')})" + (f": `{c}`" if c else ""))
        envs = ", ".join(map(str, (tox.get("env_list") or [])[:20]))
        out.append(_simple(path, "workflow", "tox environments", f"tox is configured in {path} [tool.tox]" + (f" with env_list {envs}" if envs else "")
                           + ". " + "; ".join(parts) + ". Run `tox -e <env>`.", ["tox", "test", *list((tox.get("env") or {}).keys())[:15]],
                           text, key="py:tox", start=_line_of(text, "[tool.tox")))
    for runner in ("nox", "hatch", "pdm", "uv"):
        cfg = tool.get(runner)
        if cfg and runner in ("hatch", "pdm") and isinstance(cfg, dict) and cfg.get("scripts"):
            out.append(_simple(path, "workflow", f"{runner} scripts", f"{runner} scripts in {path}: " + ", ".join(f"{k} → {v}" for k, v in list(cfg["scripts"].items())[:12]), [runner], text, key=f"py:{runner}"))
    return out


def _flatten(obj, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_flatten(v, f"{prefix}{k}."))
    else:
        val = obj if not isinstance(obj, list) else "[" + ", ".join(map(str, obj[:8])) + "]"
        out.append(f"{prefix.rstrip('.')}={val}")
    return out[:30]


def _requirements(path: str, text: str) -> list[Candidate]:
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        names.append(_dep_name(line))
    dev = "dev" in path.lower() or "test" in path.lower()
    return _deps(path, "Python dependencies", [] if dev else names, text, names if dev else None)


def _go_mod(path: str, text: str) -> list[Candidate]:
    module = re.search(r"^module\s+(\S+)", text, re.M)
    gover = re.search(r"^go\s+(\S+)", text, re.M)
    reqs = re.findall(r"^\s*([\w.-]+\.[\w.-]+/[^\s]+)\s+v[\w.+-]+(?!.*// indirect)", text, re.M)
    out = []
    if module:
        out.append(_simple(path, "architecture", f"Go module {module.group(1)}", f"Go module `{module.group(1)}`" + (f" targeting Go {gover.group(1)}" if gover else "") + f" ({path}).", [module.group(1)], text, key="go:module"))
    out.extend(_deps(path, "Go module requirements (direct)", reqs, text))
    return out


def _cargo(path: str, text: str) -> list[Candidate]:
    data = tomllib.loads(text)
    out = []
    pkg = data.get("package") or {}
    ws = (data.get("workspace") or {}).get("members")
    if pkg or ws:
        facts = [f"crate `{pkg.get('name')}` edition {pkg.get('edition', '?')}"] if pkg else []
        if ws:
            facts.append("workspace members: " + ", ".join(ws[:15]))
        out.append(_simple(path, "architecture", f"Rust crate {pkg.get('name') or 'workspace'}", f"{path}: " + "; ".join(facts) + ".", [pkg.get("name") or "workspace"], text, key="cargo:meta"))
    out.extend(_deps(path, "Rust crates", list((data.get("dependencies") or {}).keys()), text, list((data.get("dev-dependencies") or {}).keys())))
    return out


# --------------------------------------------------------------- workflows
def _makefile(path: str, text: str) -> list[Candidate]:
    targets: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z0-9][\w.-]*)\s*:(?!=)", line)
        if not m or m.group(1).startswith("."):
            continue
        recipe = ""
        for nxt in lines[i + 1 : i + 4]:
            if nxt.startswith("\t") or nxt.startswith("    "):
                recipe = nxt.strip().lstrip("@-")
                break
        targets.append(f"`make {m.group(1)}`" + (f" → `{recipe[:100]}`" if recipe else ""))
    if not targets:
        return []
    tags = [t for t in ("build", "test", "lint", "deploy", "release", "install", "docs") if any(t in x for x in targets)]
    return [_simple(path, "workflow", f"Make targets ({path})", f"{path} defines: " + "; ".join(targets[:30]) + ".", tags, text, key="make:targets")]


def _dockerfile(path: str, text: str) -> list[Candidate]:
    froms = re.findall(r"(?im)^\s*FROM\s+(\S+)", text)
    expose = re.findall(r"(?im)^\s*EXPOSE\s+(.+)$", text)
    cmd = re.findall(r"(?im)^\s*(?:CMD|ENTRYPOINT)\s+(.+)$", text)
    workdir = re.findall(r"(?im)^\s*WORKDIR\s+(\S+)", text)
    parts = [f"base image(s) {', '.join(froms)}" if froms else ""]
    if len(froms) > 1:
        parts.append("multi-stage build")
    if workdir:
        parts.append(f"workdir {workdir[-1]}")
    if expose:
        parts.append(f"exposes {', '.join(e.strip() for e in expose)}")
    if cmd:
        parts.append(f"runs {cmd[-1].strip()[:160]}")
    return [_simple(path, "workflow", f"Container image ({path})", f"Docker image defined in {path}: " + "; ".join(p for p in parts if p) + ".", ["deployment", "docker", *froms[:4]], text, key="docker")]


def _compose(path: str, text: str) -> list[Candidate]:
    services: list[str] = []
    in_services = False
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services:
            if re.match(r"^\S", line):
                in_services = False
                continue
            m = re.match(r"^  ([\w.-]+):\s*$", line)
            if m:
                services.append(m.group(1))
    images = re.findall(r"^\s+image:\s*['\"]?([^\s'\"]+)", text, re.M)
    if not services:
        return []
    return [_simple(path, "workflow", f"Docker Compose services ({path})", f"{path} defines services: {', '.join(services)}" + (f"; images: {', '.join(images[:10])}" if images else "") + ". Start locally with `docker compose up`.", ["docker", "compose", *services], text, key="compose")]


def _gha(path: str, text: str) -> list[Candidate]:
    name = re.search(r"^name:\s*['\"]?(.+?)['\"]?\s*$", text, re.M)
    wf = name.group(1) if name else PurePosixPath(path).stem
    triggers = _yaml_on(text)
    jobs = re.findall(r"^  ([\w-]+):\s*$", text[text.find("\njobs:"):] if "\njobs:" in text else "", re.M)
    runs = []
    for m in re.finditer(r"^\s+run:\s*(\|)?\s*(.*)$", text, re.M):
        if m.group(2):
            runs.append(m.group(2).strip())
        else:
            block = text[m.end():].split("\n")
            for ln in block[1:4]:
                if ln.strip():
                    runs.append(ln.strip())
                    break
    uses = sorted(set(re.findall(r"uses:\s*([\w.-]+/[\w.-]+)", text)))
    deploy = bool(re.search(r"deploy|release|publish|pages|pypi|npm publish|docker push", (wf + " " + path + " " + " ".join(jobs) + " " + " ".join(runs)), re.I))
    content = f"GitHub Actions workflow \"{wf}\" ({path})" + (f" triggers on {triggers}" if triggers else "") + (f"; jobs: {', '.join(jobs[:12])}" if jobs else "")
    if runs:
        content += "; runs: " + "; ".join(f"`{r[:100]}`" for r in runs[:8])
    if uses:
        content += "; actions: " + ", ".join(uses[:10])
    return [_simple(path, "workflow", f"CI workflow: {wf}", content + ".", ["ci", "github-actions"] + (["deployment"] if deploy else []) + jobs[:10], text, key="gha")]


def _yaml_on(text: str) -> str:
    m = re.search(r"^(?:on|\"on\"|'on'):\s*(.*)$", text, re.M)
    if not m:
        return ""
    if m.group(1).strip():
        return m.group(1).strip()
    rest = text[m.end():].splitlines()
    events = []
    for ln in rest:
        if ln and not ln.startswith(" "):
            break
        mm = re.match(r"^  ([\w-]+):?", ln)
        if mm:
            events.append(mm.group(1))
    return ", ".join(events)


def _gitlab(path: str, text: str) -> list[Candidate]:
    stages = re.findall(r"^\s+-\s*([\w-]+)\s*$", text.split("stages:", 1)[1].split("\n\n", 1)[0], re.M) if "stages:" in text else []
    jobs = [j for j in re.findall(r"^([\w.-]+):\s*$", text, re.M) if j not in ("stages", "variables", "default", "include", "workflow") and not j.startswith(".")]
    scripts = re.findall(r"^\s+-\s+(.+)$", text, re.M)[:8]
    deploy = any(re.search(r"deploy|release|publish", x, re.I) for x in jobs + stages)
    return [_simple(path, "workflow", "GitLab CI pipeline", f"GitLab CI ({path}): stages {', '.join(stages)}; jobs {', '.join(jobs[:15])}; commands " + "; ".join(f"`{s[:80]}`" for s in scripts) + ".", ["ci", "gitlab"] + (["deployment"] if deploy else []), text, key="gitlab")]


def _deploy_target(name: str) -> str:
    return {"procfile": "Heroku-style Procfile", "fly.toml": "Fly.io", "vercel.json": "Vercel", "netlify.toml": "Netlify",
            "render.yaml": "Render", "app.yaml": "Google App Engine", "railway.json": "Railway", "heroku.yml": "Heroku",
            "serverless.yml": "Serverless Framework", "wrangler.toml": "Cloudflare Workers"}.get(name, name)


# ------------------------------------------------------------- conventions
def _editorconfig(path: str, text: str) -> list[Candidate]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string("[__root__]\n" + text)
    rules = []
    for section in parser.sections():
        if section == "__root__":
            continue
        opts = {k: v for k, v in parser[section].items() if k in ("indent_style", "indent_size", "max_line_length", "end_of_line", "charset", "trim_trailing_whitespace", "insert_final_newline")}
        if opts:
            rules.append(f"{section}: " + ", ".join(f"{k}={v}" for k, v in opts.items()))
    if not rules:
        return []
    return [_simple(path, "convention", "Editor formatting rules", f"{path} sets " + "; ".join(rules[:12]) + ".", ["formatting", "editorconfig"], text, key="editorconfig")]


def _tsconfig(path: str, text: str) -> list[Candidate]:
    stripped = re.sub(r"(?m)^\s*//.*$|/\*[\s\S]*?\*/", "", text)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    data = json.loads(stripped)
    opts = data.get("compilerOptions") or {}
    keys = ("strict", "target", "module", "moduleResolution", "jsx", "noImplicitAny", "strictNullChecks", "baseUrl", "outDir", "rootDir")
    facts = [f"{k}={opts[k]}" for k in keys if k in opts]
    if opts.get("paths"):
        facts.append("path aliases " + ", ".join(list(opts["paths"].keys())[:6]))
    if data.get("extends"):
        facts.append(f"extends {data['extends']}")
    if not facts:
        return []
    return [_simple(path, "convention", f"TypeScript compiler settings ({path})", f"{path}: " + ", ".join(facts) + ".", ["typescript"], text, key="tsconfig")]


def _ini_tools(path: str, text: str) -> list[Candidate]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    out = []
    for section in parser.sections():
        tool = section.replace("tool:", "")
        if tool in ("flake8", "pycodestyle", "isort", "mypy", "pylint", "pydocstyle", "coverage:run", "tool:pytest", "pytest"):
            opts = ", ".join(f"{k}={v.strip()}" for k, v in list(parser[section].items())[:8])
            cat = "workflow" if "pytest" in tool else "convention"
            out.append(_simple(path, cat, f"{tool} settings ({path})", f"[{section}] in {path}: {clean_text(opts, 400)}.", [tool], text, key=f"ini:{section}"))
        elif section.startswith("testenv") or section == "tox":
            cmds = parser[section].get("commands", "").strip().replace("\n", "; ")
            if cmds:
                out.append(_simple(path, "workflow", f"tox {section}", f"tox [{section}] in {path} runs: {clean_text(cmds, 300)}.", ["test", "tox"], text, key=f"ini:{section}"))
    return out
