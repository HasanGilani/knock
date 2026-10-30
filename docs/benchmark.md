# Benchmark methodology and results

Two harnesses live in `benchmarks/`:

| harness | needs | measures |
|---|---|---|
| `retrieval_bench.py` | nothing (clones the pinned repo once) | whether the facts each task needs reached the agent, how many characters and tool calls that took, indexing and refresh cost, cache latency, and a staleness scenario |
| `agent_bench.py` | an Anthropic API credential; **spends money** | real model usage per condition (input/output/cache tokens as reported by the API), tool calls, file reads, repeated reads, wall time, answer correctness, and cost computed from usage |

## Task sets

Task files are in `benchmarks/tasks/`. Each pins a repository to a tag and commit and defines 12 tasks across six types: architecture, locating a feature, deployment/workflow, investigating a documented bug, the rationale for a decision, and modifying code according to conventions. A task lists `required` regexes: the facts an engineer would need for the answer (file paths, identifiers, commands). Every pattern was checked against the pinned commit.

| set | repository | role |
|---|---|---|
| `flask.json` | pallets/flask 3.1.2 (`2c1b30d0`) | **Development set.** Extractors and ranking were adjusted while looking at these results (for example class attributes as symbols, commands first in workflow items, tox-in-pyproject extraction). These numbers are optimistic. |
| `fastapi-template.json` | fastapi/full-stack-fastapi-template 0.9.0 (`e4022a95`) | **Held-out set.** Tasks were written and checked before knock was run on this repository, and knock was not changed after seeing the results. Only a harness bug in the staleness scenario was fixed afterwards: the replacement string contained the original. That fix didn't affect task scoring. |

## Deterministic retrieval benchmark

All workflows get the same task text and the same term extraction.

- **Baseline A, "grep + read":** one `grep -il` per key term (the 3 rarest task terms present in the repository), then a full, line-numbered read (≤2,000 lines) of the top 3 files ranked by distinct terms matched.
- **Baseline B, "targeted grep":** one `grep -in` per key term (≤60 matching lines each), then a 120-line window around the densest cluster of matches in each of the top 2 files.
- **knock:** one `get_context(task)` with the default 8,000-character budget, then up to two `get_source` calls on the suggested files while facts are still missing.
- **Oracle:** the smallest single file that contains every required fact. This is a lower bound for reading one whole file. It isn't a workflow.

A task passes when every required regex occurs in the consumed text: grep output, file contents with paths, or knock's JSON responses. Characters are measured. Tokens are estimated as characters/4.

Run it:

```bash
python benchmarks/retrieval_bench.py                                          # Flask (development set)
python benchmarks/retrieval_bench.py --tasks benchmarks/tasks/fastapi-template.json
```

Outputs go to `benchmarks/results/retrieval_<repo>.{md,json}`.

### Results (re-run on 2026-10-31 after the fixes below; macOS arm64, Python 3.10, knock 0.1.0)

Before those fixes, knock scored the same tasks but returned more text: 122,506 characters on the held-out set and 107,164 on Flask.

**Held-out: full-stack-fastapi-template 0.9.0**

| workflow | passed | chars consumed | median chars/task | tool calls |
|---|---|---|---|---|
| A: grep + read 3 files | 9/12 | 1,215,570 | 117,924 | 72 |
| B: targeted grep + 2 windows | 7/12 | 328,206 | 27,086 | 60 |
| knock | 10/12 | 88,962 | 4,311 | 17 |
| oracle single file | — | 160,947 | — | — |

knock's held-out misses:

- `arch-routers`: `backend/app/api/main.py` defines `api_router = APIRouter()` at module level. knock doesn't index module-level variables (only UPPER_CASE constants), and the file has no docstring, so nothing pointed to it.
- `convention-lint`: the task says "linters, type checkers and formatters". Porter-stemmed FTS turns `linters` into `linter`, which doesn't match the stored `lint`, so the ruff/mypy/Biome items were never retrieved. (A synonym for this was deliberately *not* added, so the held-out set stays untuned.)

Baseline A missed 3 tasks (`arch-routers`, `locate-jwt`, `bug-first-start`), and baseline B missed 5.

**Development set: Flask 3.1.2** (optimistic, see above)

| workflow | passed | chars consumed | median chars/task | tool calls |
|---|---|---|---|---|
| A: grep + read 3 files | 7/12 | 2,141,848 | 180,400 | 72 |
| B: targeted grep + 2 windows | 7/12 | 246,604 | 21,842 | 60 |
| knock | 12/12 | 91,830 | 7,555 | 15 |
| oracle single file | — | 244,044 | — | — |

**Ingestion and maintenance (both repositories, ~196 eligible files each):** a cold index took 330–640 ms, and a no-op refresh took about 30 ms with 0 files reprocessed. Extraction makes 0 model calls. The database was 5–6 MB (FTS indexes dominate).

**Cache:** on Flask, `get_context` had a median latency of 72 ms uncached and 22 ms when repeated. All 12 repeats were served from the application cache.

**Staleness scenario** (both sets): a documented command is edited in a copy of the repository without re-indexing. Before refresh, knock still returned the old command but marked the response `freshness: stale` and annotated the cited source as "file modified in working tree since last index". `refresh_repository` reprocessed 1 file (195 unchanged, about 35 ms). After that, the new text was served and the old text wasn't.

### Threats to validity

- **This is a retrieval proxy, not task success.** "The fact reached the agent" doesn't mean an agent would notice it, reason correctly, or finish the task. The baselines are simulations of agent behaviour; real agents search adaptively and may do better (or worse) than either baseline.
- **Pattern grading** is lenient: a pattern can match incidental text. It can also be strict: a correct answer phrased differently fails.
- **Development-set bias:** the Flask results reflect tuning. Only the held-out results are reasonable evidence, and 12 tasks on one repository is a small sample.
- **Small repositories** (about 200 files). Behaviour on large monorepos hasn't been measured.
- **Characters aren't money.** Agent cost also includes system prompts, tool schemas (knock adds four tool definitions), reasoning, output, and prompt caching. Reading fewer characters doesn't translate one-to-one into a saving.

## Claude Code recordings (side by side)

The same two-day task, recorded in real Claude Code sessions on Flask 3.1.2 (model `claude-sonnet-5`, one run per condition, 2026-10-31). Monday: *"Users get logged out every time we rotate SECRET_KEY, even though the old key is in SECRET_KEY_FALLBACKS. Why?"* Tuesday (new session): *"Add a debug log message when a session cookie can't be verified."* The "with knock" condition had knock connected plus the one-line `CLAUDE.md` tip.

| | without knock | with knock (knock 0.1.0 before fixes) |
|---|---|---|
| Monday | 79 s · 10 tool calls · $0.21 | 164 s · 25 tool calls · $0.39 |
| Tuesday | 26 s · 7 tool calls · $0.12 | 33 s · 8 tool calls · $0.13 |

Both conditions answered correctly and made the same one-line change. **knock did not help in this recording.** The transcripts showed three knock problems, since fixed:

1. Commit messages were cut to their subject line. The fix commit's body explained the cause exactly, but knock only kept *"secret key rotation: fix key list ordering"*, so Claude went looking for a library's source (and was blocked 10 times). knock now keeps commit bodies, minus trailers and email addresses, as the rationale of fix-commit notes.
2. `get_context` filled its budget with off-topic notes (6 of 8 on Monday). Query words are now weighted by rarity in the repository, a few generic code synonyms are applied, and results scoring below 60% of the best match are dropped.
3. Code could only be found by name. Python functions now also record what they call, catch, raise and read (e.g. `open_session` catches `BadSignature`), so Tuesday's target function ranks second instead of not appearing.

Also visible: Claude without knock hit the same blocked commands (3 times), so single runs are noisy. The recordings have **not yet been repeated** with the fixed version. A fair re-test should use several runs per condition and, ideally, a codebase Claude doesn't already know.

## Model-in-the-loop benchmark (`agent_bench.py`)

This benchmark uses the same model, the same system prompt, the same 20-turn limit and the same starting state. The **baseline** gets `list_files`/`grep`/`read_file`. The **knock** condition gets the same tools plus `get_context`/`search_knowledge`/`get_knowledge`/`get_source`, served in-process by the same service the MCP server uses. The order of the two conditions alternates per task. Prompt caching is enabled identically in both conditions (top-level automatic `cache_control`).

Per run it records, from `response.usage`: `input_tokens`, `output_tokens`, `cache_read_input_tokens` and `cache_creation_input_tokens`. It also records tool calls, file reads, repeated reads of the same file, wall time, stop reasons, and pass/fail, where every required pattern must appear in the final answer. Cost is computed from measured usage and a price table in the script; check current prices before quoting it. Server-side refusal fallbacks are on by default (`--no-fallbacks` to disable). Runs that a fallback model served are flagged and excluded from the summary.

```bash
pip install -e '.[bench]'
python benchmarks/agent_bench.py --tasks benchmarks/tasks/fastapi-template.json --confirm-spend --repeats 3
```

Without `--confirm-spend` it only prints the planned number of runs.

**Status: not run.** No results are reported, because none were obtained. The harness's local tools were exercised, and its request parameters were checked against the installed `anthropic` SDK's signatures. No API request has been made with it.
