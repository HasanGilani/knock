# Connecting agents

knock exposes an MCP server over **stdio**:

```bash
knock serve --repo /path/to/repo [--repo /path/to/other] [--no-refresh]
```

- `--repo` can be repeated. The server only answers for the repositories listed. With one repository, the `repository` tool argument is optional.
- At start-up the server begins serving immediately and runs an incremental refresh in the background (`--no-refresh` skips it). During that refresh, freshness reports `indexing`.
- Logs are JSON lines on stderr (stdout carries the protocol). Set `KNOCK_LOG_LEVEL=DEBUG` for more.
- Set `KNOCK_HOME` to use a different data directory.

## Compatibility status

| client | status |
|---|---|
| MCP Python SDK `Client` (in-process and stdio subprocess) | tested in `tests/test_mcp.py` |
| Claude Code 2.1.267 (macOS) | **tested end to end**: `claude mcp add` connects, and in a real `claude -p` session (model `claude-sonnet-5`, Flask 3.1.2) Claude called `get_context` first, then `get_source` twice, and answered correctly in 8 turns. The README demo shows this session. `record_knowledge` was available but not used in that run. |
| Cursor, Windsurf, VS Code, Claude Desktop, others | should work with any stdio MCP client; **untested** |

## Claude Code

From the repository you want to work on:

```bash
claude mcp add knock -- /absolute/path/to/knock/.venv/bin/knock serve --repo . --toolset map
```

Or commit a project-scoped `.mcp.json` (each user needs `knock` on their `PATH`):

```json
{
  "mcpServers": {
    "knock": { "command": "knock", "args": ["serve", "--repo", "."] }
  }
}
```

Optionally, tell the agent when to use it, for example in `CLAUDE.md`:

```markdown
Use the knock MCP tools when you would otherwise explore the repository (how/why questions, unclear bugs, changes
across modules, code you can't find with one search); skip them for small edits you can locate with one grep.
Call get_context with the task first, verify details
with get_source before editing, and record reusable findings (root causes, non-obvious workflows, decisions) with
record_knowledge, citing the files that support them.
```

## Other stdio MCP clients

Most clients accept the same shape:

```json
{
  "mcpServers": {
    "knock": {
      "command": "/absolute/path/to/knock/.venv/bin/knock",
      "args": ["serve", "--repo", "/absolute/path/to/repo"]
    }
  }
}
```

## Compact "map" toolset (prototype)

`knock serve --repo . --toolset map` exposes three tools instead of eight, with plain-text answers:

| tool | what it returns |
|---|---|
| `map(task)` | the code relevant to a task as a skeleton: signatures, exact line ranges, what each function uses and catches, who calls it; plus commit/decision history, earlier findings, and the best doc or config sections. Usually 1–4K characters. Re-indexes changed files itself. |
| `open(target)` | exact, line-numbered code for a symbol (`open_session`, `Class.method`) with what it calls and who calls it; or `path:start-end`; or a file's skeleton |
| `remember(note, files?)` | saves a finding; `files` may be `"src/app.py"` or `"src/app.py:120-160"` strings |

Suggested `CLAUDE.md` line: *"Before exploring the repo (how/why questions, unclear bugs, changes across modules, code you can't find with one search), call knock's map tool with your task. For small edits you can locate with one grep, skip it. Save non-obvious findings with remember."*

Why the "skip it" part: in recorded Claude Code sessions, a small edit whose file one regex finds takes 3 steps without knock (grep, Read, Edit) and at least 4 with it (loading knock's tools, map, Read, Edit). On a multi-module investigation in a 4,776-file repository, knock cut the median cost from $0.49 to $0.29. See [benchmark.md](benchmark.md).

The same views are available from the CLI: `knock map "task"` and `knock open <target>`.

## Tool reference

All tools return structured content plus the same data as compact JSON text. Every read response includes `freshness` (`fresh`, `stale`, `indexing`, `incomplete` or `never_indexed`, plus revisions). Every knowledge item includes `sources` (path, lines, and an `evidence` warning when the source changed) and `provenance`.

| tool | arguments | notes |
|---|---|---|
| `get_context` | `task`, `paths?`, `max_chars?` (1,000–60,000; default 8,000) | knowledge, `symbols`, `files_to_inspect`, `recent_changes`, `gaps`, `guidance` |
| `search_knowledge` | `query`, `categories?`, `path_prefix?`, `include_inactive?`, `limit?` (≤25), `max_chars?` | `omitted_results`, `notes` (unmatched terms, collapsed duplicates, stale warnings) |
| `get_knowledge` | `knowledge_id`, `include_evidence?` | full item, all sources with hashes, links, optional excerpts |
| `get_source` | `path`, `start_line?`, `end_line?`, `symbol?` | ≤200 lines; `changed_since_index`, `truncated`/`next_start_line`, `redactions` |
| `record_knowledge` | `category`, `content`, `title?`, `rationale?`, `sources?` [`{path,start_line?,end_line?}`], `entities?`, `confidence?`, `supersedes?` | returns `outcome` `created`/`deduplicated`, `possible_conflicts`, `superseded` |
| `update_knowledge_status` | `knowledge_id`, `action` `verify`/`invalidate`, `reason?` | verify only applies to agent-recorded items |
| `get_repository_status` | – | freshness, counts, recent runs, cache stats, usage counters |
| `refresh_repository` | `full?` | change counts, knowledge changes, duration, `model_calls: 0` |

Read tools are annotated `readOnlyHint: true`. Mutating tools are annotated `readOnlyHint: false, destructiveHint: false`. Errors come back as tool errors with a plain message, never a traceback.

## Good agent habits

- Use knock when you'd otherwise explore: start with `get_context` (or `map`), then open only the files it suggests, and only the lines you need.
- Skip knock for small edits whose location one search finds; it adds steps there without saving any.
- Treat `stale` items and `freshness.state != "fresh"` as hints that need verifying. Call `refresh_repository` after editing files if you'll query again.
- Record one fact per item. Cite sources. Never record secrets, speculation or temporary state.
- When an item turns out to be wrong, `update_knowledge_status(..., "invalidate", reason)` it, or `record_knowledge(..., supersedes=[id])` with the correction.
