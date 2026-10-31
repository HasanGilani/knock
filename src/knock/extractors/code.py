"""Source-code extraction: symbols, module docs, routes, entry points.

Python uses the standard-library ``ast``; other languages use conservative
line-oriented patterns. Comments other than a file's leading doc block are not
treated as knowledge (they are often stale or temporary).
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from ..util import first_sentences
from . import Candidate, FileExtraction, Symbol

MAX_SYMBOLS_LISTED = 24
_TEST_HINT = re.compile(r"(^|/)(tests?|__tests__|spec|specs)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")
_LICENSE_HINT = re.compile(r"(?i)copyright|licen[cs]e|spdx-license")
_ROUTE_DECORATOR = re.compile(r"^\s*@\s*[\w.]+\.(route|get|post|put|patch|delete|websocket|api_route)\(\s*['\"]([^'\"]+)['\"]")


def is_test_path(path: str) -> bool:
    return bool(_TEST_HINT.search(path.lower()))


def extract(path: str, text: str, language: str) -> FileExtraction:
    if language == "python":
        try:
            result = _python(path, text)
        except (SyntaxError, ValueError, RecursionError):
            result = _regex(path, text, language)
    else:
        result = _regex(path, text, language)
    summary = _file_summary(path, text, language, result)
    if summary:
        result.candidates.append(summary)
    return result


# --------------------------------------------------------------------- python
def _python(path: str, text: str) -> FileExtraction:
    tree = ast.parse(text)
    out = FileExtraction()
    out.doc = (ast.get_docstring(tree) or "").strip()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(_expr(b) for b in node.bases)
            out.symbols.append(Symbol(node.name, "class", node.lineno, f"class {node.name}({bases})" if bases else f"class {node.name}",
                                      refs=[_expr(b) for b in node.bases], end_line=node.end_lineno, doc=_doc1(node)))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _dunder_noise(item.name):
                    out.symbols.append(_py_function(f"{node.name}.{item.name}", "method", item))
                elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                    for tgt in targets:
                        if isinstance(tgt, ast.Name) and not tgt.id.startswith("__"):
                            out.symbols.append(Symbol(f"{node.name}.{tgt.id}", "attribute", item.lineno, f"{node.name}.{tgt.id}",
                                                      end_line=item.end_lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.symbols.append(_py_function(node.name, "function", node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for tgt in targets:
                if not isinstance(tgt, ast.Name) or tgt.id.startswith("__"):
                    continue
                name = tgt.id
                value = node.value
                if name.isupper() and len(name) > 2:
                    out.symbols.append(Symbol(name, "constant", node.lineno, name, end_line=node.end_lineno))
                elif isinstance(value, ast.Call):
                    # Module-level objects such as `app = Flask(__name__)` or `api_router = APIRouter()`.
                    out.symbols.append(Symbol(name, "variable", node.lineno, f"{name} = {_expr(value.func)}(…)",
                                              refs=_py_refs(node), end_line=node.end_lineno))
        elif isinstance(node, ast.If) and _is_main_guard(node.test):
            out.symbols.append(Symbol("__main__", "entrypoint", node.lineno, 'if __name__ == "__main__"', end_line=node.end_lineno))
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _ROUTE_DECORATOR.match(line)
        if m:
            method = m.group(1).upper() if m.group(1) not in ("route", "api_route") else "ROUTE"
            out.symbols.append(Symbol(f"{method} {m.group(2)}", "route", lineno, line.strip()[:160]))
    return out


def _py_function(name: str, kind: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Symbol:
    catches: list[str] = []
    for h in ast.walk(fn):
        if isinstance(h, ast.ExceptHandler) and h.type is not None:
            for t in (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]):
                label = _expr(t)
                if label not in catches:
                    catches.append(label)
    start = min([d.lineno for d in fn.decorator_list] + [fn.lineno])
    return Symbol(name, kind, start, _py_sig(fn), _py_refs(fn), end_line=fn.end_lineno, doc=_doc1(fn), catches=catches[:8])


def _doc1(node: ast.AST) -> str:
    try:
        doc = ast.get_docstring(node) or ""
    except TypeError:
        return ""
    first = doc.strip().split("\n\n")[0].replace("\n", " ")
    return first_sentences(first, 140)


_REF_NOISE = frozenset("""self cls super len str int float bool list dict set tuple isinstance getattr setattr hasattr
    print range enumerate zip map filter sorted min max any all repr type iter next append extend update pop items
    keys values format join split strip startswith endswith lower upper replace encode decode copy""".split())
_CONFIG_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{2,60}$")
MAX_REFS = 40


def _py_refs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Names a function's body depends on: calls, attributes, caught/raised exceptions, string keys.

    This lets a search for what code *does* ("where is a bad signature handled", "what reads
    SECRET_KEY_FALLBACKS") find a function whose own name says nothing about it.
    """
    seen: dict[str, None] = {}

    def add(name: str | None) -> None:
        if name and name not in _REF_NOISE and not name.startswith("__") and len(seen) < MAX_REFS:
            seen.setdefault(name, None)

    def names_in(node: ast.AST | None) -> None:
        if isinstance(node, ast.Name):
            add(node.id)
        elif isinstance(node, ast.Attribute):
            add(node.attr)
        elif isinstance(node, ast.Tuple):
            for elt in node.elts:
                names_in(elt)

    for node in ast.walk(fn):
        if isinstance(node, ast.ExceptHandler):
            names_in(node.type)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            names_in(node.exc.func if isinstance(node.exc, ast.Call) else node.exc)
        elif isinstance(node, ast.Call):
            names_in(node.func)
        elif isinstance(node, ast.Attribute):
            add(node.attr)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id[:1].isupper():
            add(node.id)  # constants (TAX_RATE) and classes (BadSignature); local variables stay out
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            if _CONFIG_KEY.match(node.slice.value):
                add(node.slice.value)
    return list(seen)


def _dunder_noise(name: str) -> bool:
    return name.startswith("__") and name.endswith("__") and name not in ("__init__", "__call__")


def _py_sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [a.arg for a in node.args.posonlyargs + node.args.args if a.arg not in ("self", "cls")]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    args.extend(a.arg for a in node.args.kwonlyargs)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(args)})"[:200]


def _expr(node: ast.expr) -> str:
    try:
        return ast.unparse(node)[:60]
    except Exception:  # pragma: no cover - defensive
        return "?"


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in test.comparators)
    )


# ---------------------------------------------------------------- other langs
_JS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type)\s+([A-Za-z_$][\w$]*)\b"), "type"),
    (re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)"), "enum"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"), "function"),
    (re.compile(r"^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)"), "export"),
]
_PATTERNS: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "javascript": _JS,
    "typescript": _JS,
    "vue": _JS,
    "svelte": _JS,
    "go": [
        (re.compile(r"^func\s+\(\s*\w+\s+\*?(\w+)[^)]*\)\s+(\w+)\s*[\[(]"), "method"),
        (re.compile(r"^func\s+(\w+)\s*[\[(]"), "function"),
        (re.compile(r"^type\s+(\w+)\s+(?:struct|interface)\b"), "type"),
    ],
    "rust": [
        (re.compile(r"^\s*(?:pub(?:\([\w:]+\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)"), "function"),
        (re.compile(r"^\s*(?:pub(?:\([\w:]+\))?\s+)?(?:struct|enum|trait|union)\s+(\w+)"), "type"),
        (re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?([\w:]+)"), "impl"),
        (re.compile(r"^\s*(?:pub\s+)?mod\s+(\w+)"), "module"),
    ],
    "java": [
        (re.compile(r"^\s*(?:(?:public|protected|private|abstract|final|static|sealed)\s+)*(?:class|interface|enum|record)\s+(\w+)"), "class"),
        (re.compile(r"^\s+(?:(?:public|protected|private|static|final|abstract|synchronized)\s+)+[\w<>\[\], ?]+\s+(\w+)\s*\([^;]*$"), "method"),
    ],
    "kotlin": [
        (re.compile(r"^\s*(?:(?:public|internal|private|open|abstract|data|sealed|enum)\s+)*(?:class|interface|object)\s+(\w+)"), "class"),
        (re.compile(r"^\s*(?:(?:public|internal|private|override|suspend|inline)\s+)*fun\s+(?:<[^>]+>\s*)?(?:[\w.]+\.)?(\w+)\s*\("), "function"),
    ],
    "csharp": [
        (re.compile(r"^\s*(?:(?:public|internal|private|protected|abstract|sealed|static|partial)\s+)*(?:class|interface|struct|enum|record)\s+(\w+)"), "class"),
        (re.compile(r"^\s+(?:(?:public|internal|private|protected|static|virtual|override|async)\s+)+[\w<>\[\], ?]+\s+(\w+)\s*\("), "method"),
    ],
    "ruby": [
        (re.compile(r"^\s*(?:class|module)\s+([\w:]+)"), "class"),
        (re.compile(r"^\s*def\s+(?:self\.)?([\w]+[?!=]?)"), "function"),
    ],
    "php": [
        (re.compile(r"^\s*(?:(?:abstract|final)\s+)?(?:class|interface|trait|enum)\s+(\w+)"), "class"),
        (re.compile(r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+(\w+)"), "function"),
    ],
    "swift": [
        (re.compile(r"^\s*(?:(?:public|private|internal|open|final)\s+)*(?:class|struct|enum|protocol|extension|actor)\s+(\w+)"), "type"),
        (re.compile(r"^\s*(?:(?:public|private|internal|open|static|override|final)\s+)*func\s+(\w+)"), "function"),
    ],
    "scala": [
        (re.compile(r"^\s*(?:case\s+)?(?:class|object|trait)\s+(\w+)"), "class"),
        (re.compile(r"^\s*def\s+(\w+)"), "function"),
    ],
    "c": [
        (re.compile(r"^(?:typedef\s+)?struct\s+(\w+)\s*\{"), "type"),
        (re.compile(r"^[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;{]*\)\s*\{?\s*$"), "function"),
    ],
    "cpp": [
        (re.compile(r"^\s*(?:class|struct)\s+(\w+)[^;]*$"), "type"),
        (re.compile(r"^[A-Za-z_][\w\s\*&:<>,]*?\b([\w:~]+)\s*\([^;{]*\)\s*(?:const\s*)?\{?\s*$"), "function"),
    ],
    "shell": [(re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?"), "function")],
    "elixir": [
        (re.compile(r"^\s*defmodule\s+([\w.]+)"), "module"),
        (re.compile(r"^\s*defp?\s+(\w+[?!]?)"), "function"),
    ],
    "dart": [(re.compile(r"^\s*(?:abstract\s+)?class\s+(\w+)"), "class")],
    "lua": [(re.compile(r"^\s*(?:local\s+)?function\s+([\w.:]+)"), "function")],
    "sql": [(re.compile(r"(?i)^\s*create\s+(?:or\s+replace\s+)?(table|view|function|procedure|index)\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)"), "sql")],
    "protobuf": [(re.compile(r"^\s*(?:message|service|enum)\s+(\w+)"), "type"), (re.compile(r"^\s*rpc\s+(\w+)"), "rpc")],
    "graphql": [(re.compile(r"^\s*(?:type|input|interface|enum|union)\s+(\w+)"), "type")],
    "terraform": [(re.compile(r"^\s*(resource|module|data)\s+\"([^\"]+)\"(?:\s+\"([^\"]+)\")?"), "terraform")],
}
_C_NOT_FUNCS = {"if", "for", "while", "switch", "return", "sizeof", "else", "do", "case"}
_JS_ROUTE = re.compile(r"\b(?:app|router|server|api|route[rs]?)\.(get|post|put|patch|delete|all|use)\(\s*['\"`]([^'\"`]+)['\"`]")
_GO_ROUTE = re.compile(r"\.(?:HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete)\(\s*\"([^\"]+)\"")


def _regex(path: str, text: str, language: str) -> FileExtraction:
    out = FileExtraction()
    out.doc = _leading_comment(text)
    patterns = _PATTERNS.get(language, [])
    in_block_comment = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if len(line) > 400:
            continue
        stripped = line.strip()
        if stripped.startswith("/*"):
            in_block_comment = "*/" not in stripped
            continue
        if in_block_comment:
            in_block_comment = "*/" not in stripped
            continue
        if stripped.startswith(("//", "#", "*", "--")) and language not in ("c", "cpp"):
            continue
        for pattern, kind in patterns:
            m = pattern.match(line)
            if not m:
                continue
            if kind == "sql":
                name, kind = m.group(2).strip('"'), m.group(1).lower()
            elif kind == "terraform":
                name = ".".join(g for g in m.groups()[1:] if g)
                kind = m.group(1)
            elif kind == "method" and language == "go":
                name = f"{m.group(1)}.{m.group(2)}"
            else:
                name = m.group(1)
            if language in ("c", "cpp", "java", "csharp") and name in _C_NOT_FUNCS:
                continue
            out.symbols.append(Symbol(name, kind, lineno, stripped[:160]))
            break
        if language in ("javascript", "typescript"):
            m = _JS_ROUTE.search(line)
            if m and m.group(2).startswith("/"):
                out.symbols.append(Symbol(f"{m.group(1).upper()} {m.group(2)}", "route", lineno, stripped[:160]))
        elif language == "go":
            m = _GO_ROUTE.search(line)
            if m and m.group(1).startswith("/"):
                out.symbols.append(Symbol(f"ROUTE {m.group(1)}", "route", lineno, stripped[:160]))
    if language == "go" and re.search(r"^package main\b", text, re.M) and re.search(r"^func main\(\)", text, re.M):
        out.symbols.append(Symbol("main", "entrypoint", 1, "package main / func main()"))
    return out


def _leading_comment(text: str) -> str:
    """The first comment block of a file, unless it is a licence header."""
    lines = text.splitlines()[:60]
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith("#!") or lines[i].strip().startswith(("'use ", '"use ', "package "))):
        i += 1
    block: list[str] = []
    if i < len(lines) and lines[i].strip().startswith("/*"):
        for line in lines[i:]:
            block.append(line.strip().lstrip("/*").rstrip("*/").strip(" *"))
            if "*/" in line:
                break
    else:
        for line in lines[i:]:
            s = line.strip()
            if s.startswith(("//", "#", "--")):
                block.append(s.lstrip("/#-! ").strip())
            else:
                break
    doc = " ".join(b for b in block if b).strip()
    if not doc or _LICENSE_HINT.search(doc[:200]):
        return ""
    return doc


# --------------------------------------------------------------- file summary
def _file_summary(path: str, text: str, language: str, ex: FileExtraction) -> Candidate | None:
    if not ex.symbols and not ex.doc:
        return None
    by_kind: dict[str, list[str]] = {}
    for s in ex.symbols:
        if s.kind in ("method", "attribute"):
            continue
        by_kind.setdefault(s.kind, []).append(s.name)
    parts: list[str] = []
    if ex.doc:
        parts.append(first_sentences(ex.doc, 300))
    listed = 0
    for kind in ("entrypoint", "route", "class", "type", "function", "enum", "impl", "module", "export"):
        names = by_kind.pop(kind, [])
        if not names or listed >= MAX_SYMBOLS_LISTED:
            continue
        take = names[: max(1, MAX_SYMBOLS_LISTED - listed)]
        listed += len(take)
        more = f" (+{len(names) - len(take)} more)" if len(names) > len(take) else ""
        label = {"entrypoint": "Entry point", "route": "Routes", "class": "Classes", "type": "Types",
                 "function": "Functions", "enum": "Enums", "impl": "Impls", "module": "Modules", "export": "Exports"}[kind]
        if kind == "entrypoint":
            parts.append("Entry point (runnable).")
        else:
            parts.append(f"{label}: {', '.join(take)}{more}.")
    for kind, names in by_kind.items():
        if kind in ("constant",) or listed >= MAX_SYMBOLS_LISTED:
            continue
        parts.append(f"{kind}: {', '.join(names[:6])}.")
    lines = text.count("\n") + 1
    test = is_test_path(path)
    name = PurePosixPath(path).name
    entities = sorted({s.name.split(".")[-1] for s in ex.symbols if s.kind not in ("method", "constant", "attribute")})[:40]
    if test:
        entities.append("test")
    return Candidate(
        key="file-summary",
        category="implementation",
        title=f"{'Tests' if test else 'Module'} {path}",
        content=f"{path} ({language}, {lines} lines): " + " ".join(parts),
        entities=[name, *entities],
        start_line=1,
        end_line=lines,
        confidence=0.85,
    )
