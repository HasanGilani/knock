"""Repository-level knowledge derived from the index itself (layout, entry points, test and naming conventions).

These items are stored under the pseudo-path ``.`` (the repository root) and are
recomputed after every index run that changed anything; unchanged content is a no-op.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import PurePosixPath

from ..util import first_sentences
from . import Candidate
from .code import is_test_path

GROUPING_DIRS = {"src", "packages", "apps", "services", "lib", "libs", "internal", "cmd", "pkg", "modules", "components", "crates", "app"}
TEST_FRAMEWORKS = {
    "pytest": "pytest", "jest": "Jest", "vitest": "Vitest", "mocha": "Mocha", "jasmine": "Jasmine",
    "rspec": "RSpec", "phpunit": "PHPUnit", "junit": "JUnit", "@playwright/test": "Playwright", "cypress": "Cypress",
    "unittest": "unittest", "nose2": "nose2", "ava": "AVA", "tap": "node-tap",
}


def derive(files: list[dict], symbols: list[dict], docs: dict[str, str], dependency_names: set[str]) -> tuple[list[Candidate], dict[str, list[str]]]:
    """``files``: dicts with path/language/kind/size. ``symbols``: path/name/kind. ``docs``: path -> module doc."""
    cands: list[Candidate] = []
    extra: dict[str, list[str]] = {}
    if not files:
        return cands, extra
    sym_count = Counter(s["path"] for s in symbols if s["kind"] not in ("method", "attribute"))
    by_dir: dict[str, list[dict]] = defaultdict(list)
    langs = Counter(f["language"] for f in files if f["kind"] == "code")
    root_files = []
    for f in files:
        parts = f["path"].split("/")
        if len(parts) == 1:
            root_files.append(f["path"])
            continue
        by_dir[parts[0] + "/"].append(f)
        if parts[0] in GROUPING_DIRS and len(parts) > 2:
            by_dir["/".join(parts[:2]) + "/"].append(f)

    # 1. Layout
    total_code = sum(langs.values()) or 1
    lang_txt = ", ".join(f"{lang} {n * 100 // total_code}%" for lang, n in langs.most_common(6))
    top = sorted((d for d in by_dir if d.count("/") == 1), key=lambda d: -len(by_dir[d]))
    dir_txt = "; ".join(f"{d} ({len(by_dir[d])} files{_dominant(by_dir[d])})" for d in top[:15])
    notable = [p for p in root_files if not p.startswith(".")][:15]
    cands.append(Candidate(
        key="repo:layout", category="architecture", title="Repository layout",
        content=f"Indexed {len(files)} files. Languages: {lang_txt or 'n/a'}. Top-level directories: {dir_txt or 'none'}."
        + (f" Root files: {', '.join(notable)}." if notable else ""),
        entities=["layout", "structure", *[d.rstrip('/') for d in top[:15]]], confidence=0.95,
    ))

    # 2. Directory summaries
    for d, members in sorted(by_dir.items()):
        code = [f for f in members if f["kind"] == "code"]
        if len(members) < 2 or not code:
            continue
        if d.count("/") > 1 and len(code) < 2:
            continue
        key_files = sorted(code, key=lambda f: (-(sym_count.get(f["path"], 0)), -f["size"]))[:6]
        described = []
        for f in key_files:
            name = f["path"][len(d):]
            doc = docs.get(f["path"])
            described.append(f"{name} — {first_sentences(doc, 90)}" if doc else name)
        tests = sum(1 for f in code if is_test_path(f["path"]))
        content = (
            f"Directory {d} contains {len(members)} indexed files ({len(code)} code{_dominant(code)}"
            + (f", {tests} test files" if tests else "") + "). Key modules: " + "; ".join(described) + "."
        )
        cands.append(Candidate(key=f"dir:{d}", category="architecture", title=f"Directory {d}", content=content,
                               entities=[d.rstrip("/").split("/")[-1], "directory"], confidence=0.85))
        extra[f"dir:{d}"] = [f["path"] for f in key_files]

    # 3. Entry points
    entry = [s for s in symbols if s["kind"] == "entrypoint"]
    if entry:
        paths = sorted({s["path"] for s in entry})
        cands.append(Candidate(key="repo:entrypoints", category="architecture", title="Entry points",
                               content="Runnable entry points (main guards / main packages): " + ", ".join(paths[:20]) + ".",
                               entities=["entrypoint", "main"], confidence=0.9))
        extra["repo:entrypoints"] = paths[:20]

    # 4. Tests
    test_files = [f["path"] for f in files if f["kind"] == "code" and is_test_path(f["path"])]
    if test_files:
        dirs = Counter(t.split("/")[0] + "/" if "/" in t else "(root)" for t in test_files)
        patterns = Counter(_test_pattern(t) for t in test_files)
        fw = sorted({label for dep, label in TEST_FRAMEWORKS.items() if dep in dependency_names})
        content = (
            f"{len(test_files)} test files; located in {', '.join(f'{d} ({n})' for d, n in dirs.most_common(5))}; "
            f"naming: {', '.join(p for p, _ in patterns.most_common(3))}."
            + (f" Test framework(s) declared: {', '.join(fw)}." if fw else "")
        )
        cands.append(Candidate(key="repo:tests", category="convention", title="Test layout and naming", content=content,
                               entities=["test", "tests", *[f.lower() for f in fw]], confidence=0.9))
        extra["repo:tests"] = test_files[:5]

    # 5. Naming conventions per language
    by_lang = defaultdict(lambda: Counter())
    lang_of = {f["path"]: f["language"] for f in files}
    for s in symbols:
        lang = lang_of.get(s["path"])
        name = s["name"].split(".")[-1]
        style = _case(name.lstrip("_"))
        if style is None:
            continue  # a single lowercase word fits several conventions
        if s["kind"] in ("function", "method") and not name.startswith("__"):
            by_lang[(lang, "functions")][style] += 1
        elif s["kind"] in ("class", "type"):
            by_lang[(lang, "classes/types")][style] += 1
    rules = []
    for (lang, kind), counter in sorted(by_lang.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
        n = sum(counter.values())
        if n < 15:
            continue
        style, count = counter.most_common(1)[0]
        if style != "other" and count / n >= 0.7:
            rules.append(f"{lang} {kind} use {style} ({count * 100 // n}% of {n})")
    if rules:
        cands.append(Candidate(key="repo:naming", category="convention", title="Naming conventions (observed)",
                               content="Observed naming in indexed code: " + "; ".join(rules) + ".",
                               entities=["naming", "style"], confidence=0.8))

    # 6. Documentation map
    doc_files = [f["path"] for f in files if f["kind"] == "doc" and f["language"] in ("markdown", "rst")]
    if doc_files:
        groups = Counter(p.split("/")[0] + "/" if "/" in p else p for p in doc_files)
        cands.append(Candidate(key="repo:docs", category="architecture", title="Documentation map",
                               content=f"{len(doc_files)} documentation files: " + ", ".join(f"{g} ({n})" if n > 1 else g for g, n in groups.most_common(12)) + ".",
                               entities=["docs", "documentation"], confidence=0.9))
    return cands, extra


def _dominant(files: list[dict]) -> str:
    langs = Counter(f["language"] for f in files if f["kind"] == "code")
    if not langs:
        return ""
    lang, _ = langs.most_common(1)[0]
    return f", mostly {lang}"


def _test_pattern(path: str) -> str:
    name = PurePosixPath(path).name
    if name.startswith("test_"):
        return "test_*.py" if name.endswith(".py") else "test_*"
    m = re.search(r"([._](test|spec)\.[a-z]+)$", name)
    if m:
        return "*" + m.group(1)
    if name.endswith("_test.go"):
        return "*_test.go"
    return f"files under {path.split('/')[0]}/"


def _case(name: str) -> str | None:
    if not name:
        return "other"
    if re.fullmatch(r"[a-z][a-z0-9]*", name):
        return None
    if re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", name):
        return "snake_case"
    if re.fullmatch(r"[a-z]+(?:[A-Z][a-z0-9]*)+", name):
        return "camelCase"
    if re.fullmatch(r"(?:[A-Z][a-z0-9]+)+", name) or re.fullmatch(r"[A-Z][a-zA-Z0-9]+", name):
        return "PascalCase"
    if re.fullmatch(r"[A-Z][A-Z0-9_]+", name):
        return "UPPER_CASE"
    return "other"
