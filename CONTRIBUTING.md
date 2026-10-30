# Contributing

Thanks for helping. knock aims to stay small, local-first and honest about what it knows.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests create throwaway Git repositories under pytest's temp directory, and one test launches the MCP server as a subprocess. No network access is needed.

## Ground rules

- **No new required services or paid APIs.** Core functionality must work offline with only Python and Git.
- **No silent network access.** Any feature that sends data off the machine must be opt-in, documented in `docs/security.md`, and covered by a test.
- **Evidence or it didn't happen.** Knowledge must carry source references. New extractors return `Candidate`s with a stable `key`, and must not store file bodies.
- **Bump `EXTRACTOR_VERSION`** in `src/knock/extractors/__init__.py` whenever extractor output changes.
- **Schema changes** go in a new entry in `MIGRATIONS` (`src/knock/db.py`). Never edit a released migration.
- **Benchmarks:** don't tune against the held-out task set (`benchmarks/tasks/fastapi-template.json`). If you change retrieval or extraction, re-run both sets and report both. Add new held-out sets for new evaluations. Never edit results by hand.
- **Repository content is untrusted.** Never execute it, and never let it change configuration.

## Pull requests

- Include tests for behaviour changes (unit tests for logic, integration tests for storage or MCP).
- Keep runtime dependencies minimal and permissively licensed (Apache-2.0 compatible).
- Describe user-visible changes in the PR, and update docs when tool arguments or outputs change.

By contributing you agree that your contributions are licensed under the Apache License 2.0.
