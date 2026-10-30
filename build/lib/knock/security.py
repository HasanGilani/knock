"""Secret detection, sensitive-path rules and path validation.

Everything read from a repository is untrusted. These helpers keep secrets out of
the knowledge store and out of tool responses, and stop path tricks from escaping
a registered repository.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path, PurePosixPath

# File names that commonly hold credentials. Matched against the basename.
SENSITIVE_NAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.ppk",
    "*.asc",
    "*.gpg",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "_netrc",
    ".git-credentials",
    ".htpasswd",
    ".pgpass",
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
    "credentials",
    "credentials.*",
    "*credentials*.json",
    "service-account*.json",
    "*.kdbx",
    "kubeconfig",
    ".dockercfg",
)
# Templates that are conventionally safe and useful (still redacted on read).
SAFE_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")
# "secret" in a config/data file name is suspicious; in source code it is usually a module name.
_SECRET_DATA_EXT = {".json", ".yaml", ".yml", ".txt", ".ini", ".toml", ".cfg", ".conf", ".properties", ".xml"}

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)"), 0),
    ("aws_access_key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"), 0),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"), 0),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), 0),
    ("stripe_key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), 0),
    ("url_credentials", re.compile(r"(?<=://)[^\s:/@'\"]+:((?!\[REDACTED\])[^\s@/'\"]{3,})(?=@)"), 1),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|secret[_-]?key|api[_-]?key|apikey|access[_-]?key|"
            r"access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key|token)\b"
            r"[\"']?\s*[:=]\s*[\"']((?!\[REDACTED\])[^\"'\s]{8,})[\"']"
        ),
        1,
    ),
)

REDACTED = "[REDACTED]"


def sensitive_reason(rel_path: str) -> str | None:
    """Return why a path should never be indexed, or ``None`` if it may be."""
    name = PurePosixPath(rel_path).name.lower()
    if any(name.endswith(s) for s in SAFE_TEMPLATE_SUFFIXES):
        return None
    for pattern in SENSITIVE_NAME_PATTERNS:
        if fnmatch.fnmatchcase(name, pattern):
            return f"sensitive file name ({pattern})"
    if "secret" in name and PurePosixPath(name).suffix in _SECRET_DATA_EXT:
        return "sensitive file name (*secret*)"
    parts = PurePosixPath(rel_path.lower()).parts
    if ".ssh" in parts or ".aws" in parts or ".gnupg" in parts:
        return "sensitive directory"
    return None


def contains_private_key(text: str) -> bool:
    return bool(_SECRET_PATTERNS[0][1].search(text))


def redact(text: str) -> tuple[str, int]:
    """Replace likely secrets with ``[REDACTED]``. Returns ``(text, redaction_count)``."""
    count = 0
    for _name, pattern, group in _SECRET_PATTERNS:
        def _sub(m: re.Match[str], group: int = group) -> str:
            nonlocal count
            count += 1
            if group == 0:
                return REDACTED
            start, end = m.span(group)
            return m.group(0)[: start - m.start()] + REDACTED + m.group(0)[end - m.start():]

        text = pattern.sub(_sub, text)
    return text, count


class PathError(ValueError):
    """Raised for a path that is not a safe, relative path inside a repository."""


def normalize_rel_path(path: str) -> str:
    """Validate a user/agent-supplied repository-relative path and return it in POSIX form."""
    if not isinstance(path, str) or not path.strip():
        raise PathError("path must be a non-empty string")
    if "\x00" in path:
        raise PathError("path contains a NUL byte")
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise PathError("path must be relative to the repository root")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise PathError("path must not contain '..'")
    if not parts:
        raise PathError("path must name a file")
    if parts[0] == ".git":
        raise PathError("paths inside .git are not accessible")
    return "/".join(parts)


def resolve_inside(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` under ``root`` and refuse symlinks or anything escaping the root."""
    rel = normalize_rel_path(rel_path)
    root_real = Path(os.path.realpath(root))
    candidate = root_real.joinpath(*rel.split("/"))
    # Refuse a symlink at any component, not just the leaf.
    cur = root_real
    for seg in rel.split("/"):
        cur = cur / seg
        if cur.is_symlink():
            raise PathError("symbolic links are not followed")
    real = Path(os.path.realpath(candidate))
    if real != root_real and root_real not in real.parents:
        raise PathError("path escapes the repository root")
    return real
