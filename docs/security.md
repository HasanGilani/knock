# Security and privacy model

knock indexes source code that may be proprietary. Its defaults are conservative.

## What stays where

- **Storage:** everything lives in one SQLite database at `$KNOCK_HOME/knock.db` (default `~/.knock`, created with mode `0700`). knock stores derived statements, symbol names and signatures, file hashes, commit subjects and changed-file lists. It doesn't store file contents.
- **Network:** knock makes **no network connections**. It has no telemetry, no update checks, no embedding or model APIs. A test (`test_no_network_access_during_index_and_queries`) fails if indexing or any query opens a socket. The MCP server speaks stdio only.
- **What reaches the agent:** whatever a connected agent retrieves through MCP enters that agent's context and, from there, its model provider. That is the point of the tool, and it's the same exposure as the agent reading the files directly. Keep sensitive material out of the index (see below) if your agent's provider shouldn't see it.
- **The optional benchmark** `benchmarks/agent_bench.py` sends repository excerpts to the Anthropic API. It's off unless run explicitly with `--confirm-spend`, and it's not part of the application.

## Secrets

1. **Never indexed (by name):** `.env` and `.env.*` (except `*.example`/`*.sample`/`*.template`/`*.dist`), keys and certificates (`*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.ppk`, `*.asc`, `*.gpg`, `id_rsa*`, `id_ed25519*`, …), `.npmrc`, `.pypirc`, `.netrc`, `.git-credentials`, `.htpasswd`, `.pgpass`, Terraform state and `*.tfvars`, `credentials*`, `service-account*.json`, `kubeconfig`, `.dockercfg`, `*secret*` data files, and anything under `.ssh/`, `.aws/` or `.gnupg/`.
2. **Never indexed (by content):** any file containing a private key block.
3. **Redacted before extraction and in every `get_source` response:** AWS access key ids, GitHub, Slack, Stripe live, Google, Anthropic and OpenAI keys, JWTs, passwords in connection URLs, and `password|secret|api_key|token… = "…"` assignments. Agent-submitted knowledge is redacted the same way before it's stored.

Redaction is pattern-based and **will miss secrets in unusual formats**. If a repository contains real credentials, add their paths to `.knockignore` or `exclude`, and treat that as a finding to fix in the repository itself.

## Untrusted input

Repository files, documentation, commit messages, and agent-recorded knowledge are all treated as **data**:

- Nothing from a repository is executed. knock never runs build scripts, package managers, hooks or tests. Git is run without a shell, with `core.fsmonitor=false`, `core.hooksPath=/dev/null`, `diff.external=` unset, `GIT_OPTIONAL_LOCKS=0` (no index writes), terminal prompts disabled, and inherited `GIT_*` variables removed. Only read-only subcommands are used (`rev-parse`, `ls-files`, `log`).
- Manifests are parsed as data (JSON/TOML/INI) or with line patterns. Python source is parsed with `ast.parse` and never imported.
- Repository content can't change knock's configuration. It can only add exclusions through `.knockignore`.
- The MCP server instructions and every `get_context` response tell agents that repository content and recorded knowledge are data, not instructions. Each item carries its provenance (`agent/unverified` etc.) so agents can weigh it. This reduces, but can't eliminate, prompt-injection risk from repository text. Treat a repository you don't trust the same way you'd treat letting an agent read it directly.
- Control characters are stripped from stored text, and recorded items are size-limited.

## Filesystem access

- `get_source` serves **only files present in the index with status `indexed`**. Excluded, ignored, sensitive, skipped (binary/secret), unknown and new paths are refused.
- Paths must be relative. Absolute paths, drive letters, `..` segments, NUL bytes and `.git/` are rejected. The resolved real path must lie inside the repository root, and a symlink at any path component is refused. Files are opened with `O_NOFOLLOW`.
- Discovery doesn't follow symlinks (symlinked files are skipped and recorded with that reason).
- knock opens repository files read-only. Its only writes are to its own database.

## Repository isolation

- Every stored row carries `repo_id`, and every query filters on it, including FTS lookups, link joins and evidence joins.
- An MCP server is started with an explicit list of repositories (`knock serve --repo …`). Tools refuse any other repository, as well as ids, sources and supersede/verify targets that belong to other repositories (`tests/test_cache_and_isolation.py`).
- Cache keys include the repository id and generation.
- The CLI (which runs as you) can access every repository registered in its `KNOCK_HOME`. Use separate `KNOCK_HOME` directories if you need hard separation between clients or projects.

## Risks of indexing sensitive repositories

- Derived statements can quote parts of documentation and configuration (up to a few hundred characters per section). Anyone who can read `~/.knock/knock.db` can read those statements.
- Redaction is best-effort, as described above.
- Commit subjects are stored. Author names and emails are not.
- Deleting knowledge: `knock forget <repo> --yes` removes everything stored for that repository. Deleting `$KNOCK_HOME` removes everything.
