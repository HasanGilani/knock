"""File discovery: which repository files are eligible for indexing.

Git repositories use ``git ls-files`` (so .gitignore semantics are exact). Non-Git
directories fall back to a walk using the built-in exclusions plus the root
.gitignore. In both cases the built-in exclusions, the configured ``exclude``
patterns, ``.knockignore`` and the sensitive-file rules are applied on top.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import gitutil
from .config import Config
from .security import sensitive_reason

EXCLUDED_DIRS = frozenset(
    """.git .hg .svn node_modules bower_components jspm_packages vendor third_party .venv venv env .env
    virtualenv __pycache__ .mypy_cache .pytest_cache .ruff_cache .tox .nox .eggs .gradle .idea .vscode
    dist build out target bin obj .next .nuxt .svelte-kit .output .turbo .parcel-cache .cache coverage
    htmlcov .nyc_output site-packages .terraform .serverless .knock _build .docusaurus storybook-static
    Pods DerivedData .dart_tool .pub-cache elm-stuff""".split()
)

EXCLUDED_FILE_PATTERNS = (
    "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock", "composer.lock", "Gemfile.lock",
    "go.sum", "uv.lock", "*.pyc", "*.pyo", "*.class", "*.o", "*.so", "*.dylib", "*.dll", "*.exe",
    "*.generated.*", "*_pb2.py", "*.pb.go", "*.snap",
)

LANGUAGES = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".swift": "swift", ".m": "objc", ".sh": "shell",
    ".bash": "shell", ".zsh": "shell", ".lua": "lua", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang",
    ".clj": "clojure", ".dart": "dart", ".vue": "vue", ".svelte": "svelte", ".sql": "sql", ".r": "r",
    ".jl": "julia", ".hs": "haskell", ".ml": "ocaml", ".fs": "fsharp", ".proto": "protobuf",
    ".graphql": "graphql", ".gql": "graphql", ".tf": "terraform",
}
DOC_EXT = {".md": "markdown", ".markdown": "markdown", ".mdx": "markdown", ".rst": "rst", ".txt": "text", ".adoc": "asciidoc"}
CONFIG_EXT = {".json": "json", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".ini": "ini", ".cfg": "ini",
              ".xml": "xml", ".gradle": "gradle", ".properties": "properties", ".conf": "conf", ".env.example": "dotenv"}
SPECIAL_NAMES = {
    "dockerfile": "dockerfile", "containerfile": "dockerfile", "makefile": "make", "gnumakefile": "make",
    "justfile": "just", "procfile": "procfile", "jenkinsfile": "groovy", "gemfile": "ruby", "rakefile": "ruby",
    "vagrantfile": "ruby", "brewfile": "ruby", ".editorconfig": "ini",
    ".nvmrc": "text", ".python-version": "text", ".tool-versions": "text",
    "codeowners": "text", ".gitattributes": "text", ".prettierrc": "json", ".eslintrc": "json",
    ".babelrc": "json", "license": "text", "license.txt": "text", "license.md": "markdown",
}


@dataclass(frozen=True)
class FileInfo:
    path: str          # repository-relative POSIX path
    size: int
    mtime_ns: int
    language: str
    kind: str          # code | doc | config


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: str


def classify(rel_path: str) -> tuple[str, str] | None:
    """Return ``(language, kind)`` for a supported file, else ``None``."""
    p = PurePosixPath(rel_path)
    name = p.name.lower()
    if name in SPECIAL_NAMES:
        lang = SPECIAL_NAMES[name]
        kind = "doc" if lang in ("markdown", "text") and name.startswith("license") else "config"
        return lang, kind
    if name.startswith("dockerfile") or name.endswith(".dockerfile"):
        return "dockerfile", "config"
    if name.startswith(".env.") and name.endswith((".example", ".sample", ".template")):
        return "dotenv", "config"
    suffix = p.suffix.lower()
    if suffix in LANGUAGES:
        return LANGUAGES[suffix], "code"
    if suffix in DOC_EXT:
        return DOC_EXT[suffix], "doc"
    if suffix in CONFIG_EXT:
        return CONFIG_EXT[suffix], "config"
    return None


class IgnoreRules:
    """A small gitignore-style matcher (used for .knockignore and configured excludes)."""

    def __init__(self, patterns: list[str]):
        # (regex, negate, dir_only, anchored)
        self.rules: list[tuple[re.Pattern[str], bool, bool, bool]] = []
        for raw in patterns:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            anchored = "/" in line
            line = line.lstrip("/")
            if line:
                self.rules.append((_glob_to_regex(line), negate, dir_only, anchored))

    def ignored(self, rel_path: str) -> bool:
        parts = rel_path.split("/")
        result = False
        for regex, negate, dir_only, anchored in self.rules:
            for i in range(1, len(parts) + 1):
                if dir_only and i == len(parts):
                    continue
                target = "/".join(parts[:i]) if anchored else parts[i - 1]
                if regex.fullmatch(target):
                    result = not negate
                    break
        return result


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        elif glob[i] == "[" and "]" in glob[i + 1:]:
            j = glob.index("]", i + 1)
            body = glob[i + 1 : j].replace("\\", "\\\\")
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = j + 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out))


def _read_ignore_file(path: Path) -> list[str]:
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 256_000:
            return path.read_text("utf-8", "replace").splitlines()
    except OSError:
        pass
    return []


def discover(root: Path, config: Config) -> tuple[list[FileInfo], list[Skipped], bool]:
    """Return ``(eligible_files, skipped, truncated)``."""
    root = Path(os.path.realpath(root))
    rules = IgnoreRules(list(config.exclude) + _read_ignore_file(root / ".knockignore"))
    candidates = gitutil.list_files(root)
    if candidates is None:
        candidates = _walk(root, IgnoreRules(_read_ignore_file(root / ".gitignore")))

    files: list[FileInfo] = []
    skipped: list[Skipped] = []
    truncated = False
    for rel in candidates:
        rel = rel.replace("\\", "/")
        reason = _excluded_reason(rel, rules)
        if reason:
            skipped.append(Skipped(rel, reason))
            continue
        kind = classify(rel)
        if kind is None:
            skipped.append(Skipped(rel, "unsupported file type"))
            continue
        try:
            st = os.lstat(root / rel)
        except FileNotFoundError:
            continue  # tracked but deleted in the working tree
        except OSError as exc:
            skipped.append(Skipped(rel, f"stat failed: {exc.strerror}"))
            continue
        if stat.S_ISLNK(st.st_mode):
            skipped.append(Skipped(rel, "symbolic link (not followed)"))
            continue
        if not stat.S_ISREG(st.st_mode):
            skipped.append(Skipped(rel, "not a regular file"))
            continue
        if st.st_size > config.max_file_bytes:
            skipped.append(Skipped(rel, f"larger than max_file_bytes ({config.max_file_bytes})"))
            continue
        if len(files) >= config.max_files:
            truncated = True
            skipped.append(Skipped(rel, "max_files limit reached"))
            continue
        files.append(FileInfo(rel, st.st_size, st.st_mtime_ns, kind[0], kind[1]))
    return files, skipped, truncated


def _excluded_reason(rel: str, rules: IgnoreRules) -> str | None:
    parts = rel.split("/")
    for part in parts[:-1]:
        if part in EXCLUDED_DIRS or part.endswith(".egg-info"):
            return f"excluded directory ({part})"
    name = parts[-1]
    for pattern in EXCLUDED_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return f"excluded file pattern ({pattern})"
    sensitive = sensitive_reason(rel)
    if sensitive:
        return sensitive
    if rules.ignored(rel):
        return "excluded by configuration or .knockignore"
    return None


def _walk(root: Path, gitignore: IgnoreRules) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(
            d for d in dirnames if d not in EXCLUDED_DIRS and not gitignore.ignored(rel_dir + d + "/x")
        )
        for f in sorted(filenames):
            rel = rel_dir + f
            if not gitignore.ignored(rel):
                out.append(rel)
    return out
