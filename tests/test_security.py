import os

import pytest

from knock.security import PathError, normalize_rel_path, redact, resolve_inside, sensitive_reason


@pytest.mark.parametrize(
    "text",
    [
        "aws = 'AKIAIOSFODNN7EXAMPLE'",
        "token: ghp_" + "a" * 36,
        "key = sk_live_" + "b" * 24,
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
        'password = "correct-horse-battery"',
        "postgres://user:hunter2pass@db.internal/app",
        "Authorization: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_redacts_common_secrets(text):
    out, n = redact(text)
    assert n >= 1
    assert "[REDACTED]" in out


def test_redaction_keeps_context():
    out, n = redact('DATABASE_URL = "postgresql://orders:hunter2secret@localhost/orders"')
    assert n == 1
    assert "postgresql://orders:[REDACTED]@localhost/orders" in out


def test_plain_code_is_not_redacted():
    text = "def password_reset(user):\n    token = make_token(user)\n    return token\n"
    assert redact(text) == (text, 0)


@pytest.mark.parametrize("path", [".env", "config/.env.production", "id_rsa", "certs/server.pem", "secrets.yaml",
                                  "deploy/prod.tfvars", ".npmrc", "home/.ssh/config", "gcp-credentials.json"])
def test_sensitive_paths(path):
    assert sensitive_reason(path)


@pytest.mark.parametrize("path", [".env.example", "src/secret_manager.py", "README.md", "src/keys.py"])
def test_non_sensitive_paths(path):
    assert sensitive_reason(path) is None


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "a/../../b", "C:\\Windows", "", ".git/config", "a\x00b"])
def test_normalize_rejects_unsafe_paths(bad):
    with pytest.raises(PathError):
        normalize_rel_path(bad)


def test_normalize_accepts_relative():
    assert normalize_rel_path("./src//app.py") == "src/app.py"
    assert normalize_rel_path("src\\app.py") == "src/app.py"


def test_resolve_inside_refuses_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, root / "link.txt")
    os.symlink(tmp_path, root / "dirlink")
    (root / "ok.txt").write_text("fine")
    assert resolve_inside(root, "ok.txt").read_text() == "fine"
    with pytest.raises(PathError):
        resolve_inside(root, "link.txt")
    with pytest.raises(PathError):
        resolve_inside(root, "dirlink/outside.txt")
