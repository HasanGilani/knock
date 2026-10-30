"""Read-only Git access.

Git is invoked without a shell, with optional locks disabled (so it never rewrites
the index) and with repository-configurable command hooks neutralised, so that
indexing an untrusted repository never executes code from it.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import logs

log = logs.get("git")

_SAFE_CONFIG = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "diff.external=",
    "-c", "core.quotePath=false",
)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C", GIT_PAGER="cat")
    return env


def run_git(root: Path, *args: str, timeout: float = 60) -> str | None:
    """Run a read-only git command; return stdout, or ``None`` if git fails or is missing."""
    cmd = ["git", *_SAFE_CONFIG, "-C", str(root), "--no-pager", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git command failed", extra={"args": args[:2], "error": str(exc)})
        return None
    if proc.returncode != 0:
        log.debug("git non-zero exit", extra={"args": args[:2], "stderr": proc.stderr[-300:].decode("utf-8", "replace")})
        return None
    return proc.stdout.decode("utf-8", "replace")


def toplevel(path: Path) -> Path | None:
    out = run_git(path, "rev-parse", "--show-toplevel")
    return Path(out.strip()) if out else None


def head_revision(root: Path) -> str | None:
    out = run_git(root, "rev-parse", "--verify", "-q", "HEAD")
    return out.strip() if out else None


def branch(root: Path) -> str | None:
    out = run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if out else None


def list_files(root: Path) -> list[str] | None:
    """Tracked plus untracked-but-not-ignored files (honours .gitignore, info/exclude, global excludes)."""
    out = run_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", timeout=120)
    if out is None:
        return None
    return sorted({p for p in out.split("\0") if p})


@dataclass
class Commit:
    sha: str
    committed_at: str
    subject: str
    files: list[str]
    body: str = ""


_TRAILER = re.compile(r"^[A-Za-z][\w-]*-by:.*$|^(Change-Id|Reviewed-on|Signed-off-by):.*$", re.M)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def clean_body(body: str, limit: int = 1200) -> str:
    """Commit body without trailers (Signed-off-by, Co-authored-by, ...) or email addresses."""
    body = _TRAILER.sub("", body)
    body = _EMAIL.sub("[email]", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:limit]


def recent_commits(root: Path, limit: int, since: str | None = None) -> list[Commit]:
    """Most recent first. Author identities are deliberately not collected."""
    rng = [f"{since}..HEAD"] if since else ["HEAD"]
    out = run_git(
        root, "log", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
        f"--max-count={limit}", "--name-only", "--format=%x1e%H%x1f%cI%x1f%s%x1f%b%x1d", *rng, "--",
    )
    if out is None and since:
        # ``since`` may no longer exist (history rewritten); fall back to plain history.
        return recent_commits(root, limit)
    commits: list[Commit] = []
    for record in (out or "").split("\x1e"):
        if not record.strip():
            continue
        header, _, files_part = record.partition("\x1d")
        parts = header.strip("\n").split("\x1f")
        if len(parts) != 4:
            continue
        files = [f for f in files_part.splitlines() if f.strip()]
        commits.append(Commit(sha=parts[0], committed_at=parts[1], subject=parts[2][:300], files=files[:200],
                              body=clean_body(parts[3])))
    return commits
