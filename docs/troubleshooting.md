# Troubleshooting and known failure modes

## Common problems

| symptom | cause / fix |
|---|---|
| `no registered repository matches …` | Run `knock index <path>` first, or pass `--repo <name or path>`. |
| `freshness.state` is `stale` | HEAD moved or files changed since the last index. Run `knock index` or call `refresh_repository`. It's incremental and usually takes milliseconds. |
| `freshness.state` is `incomplete` | The last run failed or was interrupted. Run `knock index` again. It resumes from the stored hashes. `knock status` shows the error. |
| `busy: an index run … is already in progress` | Another process is indexing the same repository. Wait. A crashed run is taken over automatically once its process is gone, or after 15 minutes without a heartbeat. |
| A file is missing from results / `get_source` refuses it | Check `knock status` → `counts.files`, and the skip reasons in the last run (`knock index --json`). Typical reasons: `.gitignore`, built-in exclusions (`node_modules`, `dist`, lockfiles…), sensitive name, `.knockignore`, binary or non-UTF-8 content, larger than `max_file_bytes`, symlink. |
| Results are irrelevant or empty | Use the repository's own vocabulary (identifiers, file names). Add `categories`/`path_prefix`. Read `notes`/`gaps`: they list terms that matched nothing. Fall back to grep; knock doesn't replace reading code. |
| Knowledge looks outdated after a big refactor | `knock index --full` re-extracts everything (ids of unchanged items are kept). |
| `database schema vN is newer than this knock build` | Upgrade knock, or point `KNOCK_HOME` at a fresh directory. |
| MCP client shows no tools | Run the configured command manually (`knock serve --repo .`). Errors go to stderr. Make sure the client launches the command from a virtualenv where `knock` is installed (use an absolute path). |
| `pytest` crashes at start-up with `'NodeId' object has no attribute '_str_cache'` | pytest 9.2 failed on Python 3.10.0 in our environment. The `dev` extra pins `pytest<9`. |

## Known failure modes

- **Vocabulary mismatch.** Retrieval is lexical (FTS5 with Porter stemming, identifier splitting and prefix matching). A query whose words never appear in the stored text misses. Example: "linters" does not reach items tagged `lint`.
- **Module-level variables are invisible.** Only UPPER_CASE constants, functions, classes, methods and class attributes become symbols. Something like `api_router = APIRouter()` can't be found by name.
- **Docs that lie.** Extracted knowledge is as correct as the documentation it came from. `derived` means "faithfully extracted", not "true". An agent that finds a doc to be wrong should invalidate the item; the invalidation persists until that section changes.
- **Undocumented rationale.** knock only finds a "why" if someone wrote it down: docs, ADRs, or recorded knowledge. `get_context` says so explicitly instead of guessing.
- **Heuristic categories.** Doc sections are categorised from heading keywords, so some land in the wrong category. Category filters can hide them; search without a filter if results seem thin.
- **Compact views shorten text.** Items longer than about 420 characters are cut in lists (`"shortened": true`). Use `get_knowledge` for the full text.
- **Working-tree drift between refreshes.** Unsaved or unindexed edits make some returned knowledge outdated. Responses flag this per source and in `freshness`, but the content itself is from the last index.
