# Architecture

knock is a single Python package with no services: a CLI and a stdio MCP server share one application service backed by one SQLite database.

```
src/knock/
  cli.py            command line (index, search, context, source, record, status, metrics, serve, forget)
  mcp_server.py     MCP tool definitions (official MCP Python SDK, stdio transport)
  service.py        the application service: scoping, validation, caching, metrics, response shaping
  indexer.py        registration, incremental indexing, run locking/recovery, freshness
  discovery.py      which files are eligible (git ls-files, exclusions, .knockignore, sensitive rules)
  extractors/       deterministic extraction: code.py, docs.py, manifests.py, repo.py (repository-level)
  knowledge.py      knowledge store: evidence, lifecycle, deduplication, conflicts
  retrieval.py      ranking, budgets, symbol search, serialization
  cache.py          versioned query cache
  metrics.py        local counters/timings
  security.py       secret redaction, sensitive paths, path validation
  gitutil.py        read-only, hardened git invocation
  db.py             schema + versioned migrations
```

## Data flow

1. **Discovery.** In a Git repository, `git ls-files --cached --others --exclude-standard` lists candidates, so `.gitignore` semantics are Git's own. Outside Git, knock walks the directory using the root `.gitignore`. On top of that, knock applies built-in exclusions (dependency/build directories, lockfiles, minified and generated files), the configured `exclude` patterns, `.knockignore`, sensitive file names, an extension allowlist, a size cap, and a symlink refusal.
2. **Change detection.** Each eligible file is compared with its row in `files`: if size and mtime are unchanged and the extractor version matches, it is skipped without being read. Otherwise it is read and hashed (SHA-256). If the hash is unchanged, only the stat data is updated. Files that disappeared are deletions. A deleted path whose hash reappears under a new path is a rename.
3. **Extraction** (only for new or changed content). Binary files, non-UTF-8 files, and files containing a private key are skipped. Other text is passed through secret redaction and then a deterministic extractor that produces *candidates* (knowledge items with a stable key inside the file) and *symbols*.
4. **Synchronisation.** `KnowledgeStore.sync_extracted` makes the stored knowledge for a file match its candidates exactly, in one transaction per file:
   - Same key and same content: verification timestamp and revision updated.
   - Same key, new content: the item is updated in place, so its id is stable.
   - Content identical to an item from another file: one item with an additional evidence row (exact deduplication).
   - Key no longer produced: the evidence row is removed, and an item left with no evidence becomes `invalidated` with a reason.
   - Agent-recorded items that cite the file at a different hash become `stale`.
5. **Repository-level derivation.** After any change, knock recomputes repository-level items from the index itself: layout, directory summaries, entry points, test layout, observed naming conventions, and a documentation map. They are stored under the pseudo-path `.`.
6. **Git history.** New commits since the last indexed revision (newest `commit_history_limit`) are stored with their changed files. Fix-like commits ("fix", "bug", "regression", "revert", …) become `debugging` items with `historical` verification. Author identities are not collected.
7. **Completion.** Only when every file and deletion has been processed is the run marked `completed` and `indexed_revision` advanced. The repository's *generation* counter is incremented, which invalidates its cached query results.

## Data model

| Table | Purpose |
|---|---|
| `repositories` | id (`r_` + hash of the real path), name, root, `indexed_revision`, `last_indexed_at`, `generation` |
| `files` | per-file hash, size, mtime, language, kind, status (`indexed`/`skipped`/`error`) and reason, redaction count, extractor version, indexed revision, short module summary |
| `knowledge` | id (`k_…`), category, title, content, rationale, entities, origin, verification, confidence, status and reason, fingerprint, observations, timestamps, `verified_revision`, `superseded_by` |
| `knowledge_sources` | evidence: item → path, line range, file content hash at the time, revision, and the extractor's stable key |
| `knowledge_links` | `supersedes` / `conflicts` relations |
| `knowledge_fts`, `symbols_fts` | FTS5 indexes (Porter stemming; identifiers are also split into words, e.g. `parseHTTPRequest` becomes parse/http/request) |
| `symbols` | name, kind, line, signature per file (no code bodies) |
| `commits`, `commit_files` | recent history (sha, date, subject, files) |
| `index_runs` | every run with mode, status, pid, heartbeat, stats and error |
| `query_cache` | versioned cache entries |
| `metrics` | local counters (count/total/max) |

The schema is versioned with `PRAGMA user_version`, and migrations live in `db.py`. A database newer than the code is refused rather than misread.

**What is not stored:** file contents. Knowledge items hold condensed statements (at most a few hundred characters per doc section, plus symbol names and signatures). `get_source` always reads the current file from disk.

### Categories

`architecture`, `decision` (with `rationale`), `dependency`, `workflow` (build/test/deploy; deployment items carry a `deployment` entity), `convention`, `implementation`, `debugging`.

### Provenance and lifecycle

| origin / verification | meaning | ranking weight |
|---|---|---|
| `extracted/derived` | deterministically extracted from a file at a known hash | 1.0 |
| `agent/verified` | recorded by an agent and later verified against sources | 0.95 |
| `agent/unverified` | recorded by an agent, not yet verified | 0.7 |
| `git/historical` | a fact about the past (a commit), true as history | 0.55 |

| status | meaning | returned by default |
|---|---|---|
| `active` | supported by its sources | yes |
| `stale` | a cited source changed or was deleted after recording | yes, with a warning and lower rank |
| `superseded` | replaced by a newer item (`superseded_by`) | no (`include_inactive`) |
| `invalidated` | no longer produced by its source, or explicitly invalidated | no (`include_inactive`) |

An explicit invalidation of an extracted item persists until that source section's content changes. Extracted items can't be "verified" by agents; they are re-derived on refresh.

## Deduplication and conflicts

- **Exact:** the fingerprint is SHA-256 of the category plus normalised content (case, punctuation and whitespace removed).
- **Near-duplicate** (agent records): candidates come from FTS in the same category; Jaccard similarity of term sets ≥ 0.8 merges the record into the existing item (observation count +1, new sources added).
- **Possible conflict:** similarity ≥ 0.45 together with a difference in negation, or in the numbers/versions stated. Both items are linked as `conflicts` and returned with `conflicts_with`. They are not merged, because the conflict may be real.

## Retrieval and ranking

`score = (0.45·bm25_rel + 0.55·term_coverage) × authority × status_weight × (0.5 + 0.5·confidence) × intent × path_hint × penalties`

- **intent**: the query is classified with keyword rules ("why/rationale" → decision, "deploy/build/test" → workflow, "bug/error/fix" → debugging, …). Matching categories get ×1.35, and "where/implemented/defined" boosts items whose evidence is code (×1.4).
- **penalties**: items supported only by test files (×0.6 unless the query mentions tests), or only by example/fixture/benchmark paths (×0.7).
- Near-identical results (Jaccard ≥ 0.85) are collapsed, and the count is reported.
- In compact list views, item content is shortened to about 420 characters (`"shortened": true`). `get_knowledge` returns the full text.

`get_context` combines ranked knowledge, symbol hits whose *name* matches the task, a list of files to open (weighted by knowledge rank and symbol hits), recent commits touching those files whose messages share terms with the task, and explicit gaps (query terms that matched nothing, or a missing rationale for a "why" question). Budget shares: files 12%, symbols 15%, commits 7%; knowledge gets the rest.

## Caching

- **Scope:** `search_knowledge` and `get_context` ranked payloads (item ids, symbols, files, commits, gaps).
- **Key:** SHA-256 of the operation, every argument that affects the result, the repository id, and the repository's **generation**.
- **Invalidation:** any change to a repository's knowledge (an index run that changed anything, `record_knowledge`, `update_knowledge_status`) increments its generation and deletes its entries. A stale entry can't be served by construction, and repositories never share entries.
- **Not cached:** freshness and the per-source "modified in working tree" annotations. They depend on the working tree, so they are recomputed on every call (stat scan, 5 s in-process TTL).
- **Bounds:** `cache_max_entries` (1,000) and `cache_max_bytes` (16 MiB), with least-recently-used eviction.
- **Measured:** hits, misses, lookup latency, and per-tool latency. In the Flask benchmark a repeated `get_context` took a median of 22 ms against 63 ms uncached, so the cache saves local milliseconds. It saves **no model tokens** by itself: the agent receives the same text. Provider-side prompt caching is a separate mechanism that knock doesn't control.

## Concurrency and recovery

- SQLite runs in WAL mode with a 30 s busy timeout, and each thread uses its own connection.
- An index run takes a per-repository lock (an `index_runs` row with the pid and a heartbeat). A second run while the owner is alive and heartbeating returns `busy`. A run whose process died, or whose heartbeat is older than 15 minutes, is marked `interrupted` and taken over.
- Each file is committed atomically, so an interrupted run loses at most the file in flight, and the next run continues from the stored hashes. Freshness reports `incomplete` until a run completes.
- `knock index --full` (or `refresh_repository(full=true)`) re-extracts every file. It is idempotent: ids of unchanged knowledge are preserved.
- `knock forget <repo> --yes` deletes everything stored for one repository. The repository itself is never modified.

## Extending extraction

Extractors are pure functions `(path, redacted_text) → candidates, symbols`. To add one, return `Candidate`s with a stable `key` and bump `EXTRACTOR_VERSION` so existing indexes re-extract once. An LLM-backed extractor could plug in at the same point. If one is added, it must be opt-in, it must disclose what leaves the machine, and its output should be stored as `unverified`, not `derived`. None exists today.

## Configuration

`$KNOCK_HOME/config.json` (default `~/.knock/config.json`). Unknown keys are rejected.

| key | default | meaning |
|---|---|---|
| `exclude` | `[]` | extra gitignore-style exclusion patterns |
| `max_file_bytes` | 512000 | larger files are skipped |
| `max_files` | 50000 | cap per repository |
| `commit_history_limit` | 300 | commits kept |
| `search_max_chars` / `context_max_chars` | 4000 / 8000 | default response budgets |
| `source_max_lines` / `source_max_chars` | 200 / 16000 | `get_source` bounds |
| `max_results` | 25 | search limit cap |
| `record_max_chars` | 2000 | max size of a recorded item |
| `cache_max_entries` / `cache_max_bytes` | 1000 / 16 MiB | cache bounds |
| `freshness_stat_limit` / `freshness_ttl_seconds` | 20000 / 5 | freshness scan bounds |
| `log_level` | INFO | also `KNOCK_LOG_LEVEL` |

Repositories can only *add* exclusions, through `.knockignore`. Repository content never changes other settings.

## Supported files

- **Code** (symbols plus a per-file summary): Python (`ast`, including class attributes, routes and `__main__` guards), JavaScript/TypeScript (functions, classes, types, arrow functions, Express-style routes), Go, Rust, Java, Kotlin, C#, Ruby, PHP, Swift, Scala, C/C++, shell, Elixir, Dart, Lua, SQL DDL, protobuf, GraphQL, Terraform. Other recognised languages are indexed for `get_source` without symbols.
- **Docs:** Markdown and reStructuredText sections (including commands from shell code blocks), ADRs (context/decision/consequences/status). CHANGELOG/LICENSE/AUTHORS-style files are indexed for `get_source` but not extracted.
- **Manifests and config:** package.json, pyproject.toml (dependencies, scripts, pytest, ruff/black/isort/mypy/pyright/coverage, tox, hatch/pdm scripts), requirements*.txt, go.mod, Cargo.toml, Gemfile, composer.json, pom.xml, Gradle, Makefile/justfile, Dockerfile, docker compose, GitHub Actions, GitLab CI, Procfile/fly/vercel/netlify/render/app.yaml/serverless/wrangler, Kubernetes manifests, .editorconfig, tsconfig, setup.cfg/tox.ini/.flake8, pre-commit, ESLint/Prettier/Biome/RuboCop/golangci/rustfmt/clang-format/stylelint/ruff/mypy/pylint configs, runtime version files.
