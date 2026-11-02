# Installing knock (for AI agents)

knock is a local MCP server (stdio) that gives a coding agent a map of one repository. It needs no API key, and knock itself makes no network calls.

## Requirements

- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended), or pip

## 1. Find the repository path

knock serves one repository. Use the **absolute path** of the project the user works on, for example `/Users/alex/code/my-project`. Ask the user if it isn't clear.

## 2. Add the server

With uv, add this to the MCP client's settings (for Cline: `cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "knock": {
      "command": "uvx",
      "args": ["knock-mcp", "serve", "--repo", "/absolute/path/to/project", "--toolset", "map"]
    }
  }
}
```

Without uv, run `pip install knock-mcp` (or `pipx install knock-mcp`) and use `"command": "knock"` with `"args": ["serve", "--repo", "/absolute/path/to/project", "--toolset", "map"]`. If the client can't find `knock` or `uvx`, use the absolute path from `which knock` or `which uvx`.

For Claude Code, from inside the project, one command does the same:

```bash
claude mcp add knock -- uvx knock-mcp serve --repo . --toolset map
```

## 3. Check it works

The server exposes three tools: `map`, `open` and `remember`.

On first start, knock indexes the repository in the background: a few seconds for a small project, a minute or two for about 5,000 files. A `map` call made before that finishes says "first index still running"; call it again shortly. After that, only changed files are re-read.

## Usage tip

Call `map` with the task in plain words before exploring the repository (how/why questions, unclear bugs, changes across modules). For a small edit you can locate with one search, skip it. Save non-obvious findings with `remember`.
