import os

from knock.config import Config
from knock.discovery import discover

from .conftest import write


def paths(root, config=None):
    files, skipped, _ = discover(root, config or Config())
    return {f.path for f in files}, {s.path: s.reason for s in skipped}


def test_respects_gitignore_and_default_exclusions(make_repo):
    root = make_repo()
    write(root, "node_modules/lib/index.js", "x")
    write(root, "notes.md", "untracked but not ignored")
    found, skipped = paths(root)
    assert "src/orders/api.py" in found
    assert "README.md" in found
    assert "notes.md" in found                      # untracked files are included
    assert "build/bundle.js" not in found           # .gitignore
    assert "debug.log" not in found                 # .gitignore
    assert "node_modules/lib/index.js" not in found  # gitignored? no: excluded by default rules
    assert ".env" not in found and ".env" in skipped
    assert "config/secrets.yaml" not in found
    assert "certs/server.pem" not in found
    assert "assets/logo.png" not in found           # unsupported type


def test_knockignore_and_configured_excludes(make_repo):
    root = make_repo()
    write(root, ".knockignore", "web/\n")
    found, skipped = paths(root, Config(exclude=["docs/adr/*.md"]))
    assert "web/client.ts" not in found
    assert "docs/adr/0001-use-postgres.md" not in found
    assert skipped["web/client.ts"].startswith("excluded by configuration")


def test_symlinks_and_large_files_skipped(make_repo, tmp_path):
    root = make_repo()
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n")
    os.symlink(outside, root / "linked.py")
    write(root, "big.py", "x = 1\n" * 50_000)
    found, skipped = paths(root, Config(max_file_bytes=100_000))
    assert "linked.py" not in found and "symbolic link" in skipped["linked.py"]
    assert "big.py" not in found and "max_file_bytes" in skipped["big.py"]


def test_non_git_directory_fallback(tmp_path):
    root = tmp_path / "plain"
    write(root, "main.py", "print('hi')\n")
    write(root, ".gitignore", "out/\n")
    write(root, "out/gen.py", "x = 1\n")
    write(root, "node_modules/a.js", "x")
    found, _ = paths(root)
    assert found == {"main.py"}
