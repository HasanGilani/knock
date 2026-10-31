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

Also visible: Claude without knock hit the same blocked commands (3 times), so single runs are noisy.

### Re-recorded after the fixes (3 runs per condition, 2026-10-31)

Same scenario, same model (`claude-sonnet-5`), knock memory reset before each "with knock" Monday. Medians, with the range in brackets; times and costs as reported by Claude Code.

| | without knock | with knock |
|---|---|---|
| Monday (bug question) | 141 s [98–158] · $0.28 [$0.20–0.31] · 16 tool calls | 127 s [124–140] · $0.27 [$0.25–0.32] · 17 tool calls |
| Tuesday (add debug log) | 24 s [21–38] · $0.07 [$0.06–0.10] · 6 tool calls | 30 s [30–45] · $0.10 [$0.09–0.15] · 7 tool calls |

All 12 sessions answered correctly (function, file, fix commit and Flask version), and all six Tuesday changes add an equivalent debug log in `open_session`.

**Result: no measurable win on this scenario.** Monday is a wash: the ranges overlap almost completely. Tuesday is about $0.03 and 6 s *worse* with knock. Memory worked (in two of three runs, Monday's saved finding was the first thing knock returned, and `open_session` was the top symbol), but this task is found by a single search in a 200-file repo Claude already knows. knock's extra steps (loading its tools, calling `get_context`, saving a finding at the end) are pure overhead there. In both conditions Claude also spent turns on shell commands the session wasn't allowed to run.

What this does and doesn't show: knock doesn't pay off on small, well-known repositories with easy tasks. Whether it pays off on large or unfamiliar codebases, where exploration is expensive, hasn't been measured yet.

## knock v2 "map" prototype (deterministic benchmark)

`map(task)` returns a code skeleton (signatures, line ranges, uses/catches, callers) plus history and doc quotes; the workflow is `map` then up to two `open` calls on its suggestions. Run on 2026-10-31 alongside v1:

| | Flask (development set) | full-stack-fastapi-template (held-out) |
|---|---|---|
| v1 `get_context` + ≤2 `get_source` | 12/12 · 93,329 chars | 11/12 · 79,663 chars |
| v2 `map` + ≤2 `open` | 9/12 · 65,304 chars | 9/12 · 29,970 chars (median 2,302 per task) |

- v2 passes the code questions (architecture, locating features, code bugs) in one call with a fraction of v1's text. It misses some "why" and symptom questions whose answer sits deep in a doc section (Flask design rationale, the template's first-start note, import-sorting config).
- The held-out set is no longer fully untuned: indexing module-level objects (`api_router = APIRouter()`) was motivated by a held-out failure. v1's held-out score rose from 10/12 to 11/12 because of it.
- Also changed while building v2: word matching now uses suffix stemming (so "logged" no longer matches "logger"), and Python functions record references to constants and classes.
- **In real Claude Code sessions (3 runs each, same Monday/Tuesday scenario, `claude-sonnet-5`, 2026-10-31):**

  | | without knock | with knock v2 (`map`/`open`/`remember`) |
  |---|---|---|
  | Monday | 99 s [85–104] · $0.25 [$0.22–0.26] · 17 tool calls | 127 s [104–187] · $0.29 [$0.21–0.42] · 18 tool calls |
  | Tuesday | 24 s [17–26] · $0.085 [$0.065–0.10] · 6 tool calls | 27 s [21–30] · $0.085 [$0.073–0.10] · 7 tool calls |

  All 12 answers were correct and all six Tuesday edits are equivalent. **v2 shows no measurable win here either.** The transcripts show why:
  - `map` + `open` replaced Claude's own `grep` + `Read` almost one-for-one: in a repository Claude already knows, finding the code was never the expensive part.
  - Monday's cost is dominated by things outside the repository's code: in all 6 Monday sessions Claude tried to read the source of the `itsdangerous` library (3–8 blocked attempts each) and re-checked the fix commit with `git show`, even though the map quoted that commit's explanation.
  - On Tuesday, all three v2 sessions still searched for how the code base logs (`grep logger.debug`), which the map does not show.
  - Loading knock's tools costs one extra step per session (`ToolSearch`).

## Large repository: Saleor (real Claude Code sessions)

Saleor 3.23.45 (commit `e5b7e5f8`, released 2026-10-28): 4,776 files, 4,411 of them Python, about 23× Flask. It ships its own `CLAUDE.md`, which Claude Code loads in both conditions. knock indexed it in 76 s cold (0 model calls), 0.3 s for a no-op refresh, with an 80 MB database.

The scenario uses the same two-day structure, built around automatic completion of fully paid checkouts. That logic spans a channel model, two Celery tasks, a custom beat schedule, settings and GraphQL validation. Answers were verified against the code before recording.
- **Monday:** *"…fully paid checkouts never turn into orders automatically… some are a few weeks old. What decides which checkouts get completed automatically, and where is that code?"* (`trigger_automatic_checkout_completion_task` skips checkouts older than `AUTOMATIC_CHECKOUT_COMPLETION_OLDEST_MODIFIED`, 30 days, and applies each channel's delay and cut-off date.)
- **Tuesday (new session):** *"…when the background job that auto-completes paid checkouts runs, log how many checkouts it queued for completion."*

The setup was the same as before (`claude-sonnet-5`, 3 runs each, the same file tools, a fresh copy of the knock index per run). Medians, with ranges in brackets, as reported by Claude Code (2026-10-31):

| | without knock | with knock v2 (`map`/`open`/`remember`) |
|---|---|---|
| Monday cost | $0.49 [$0.44–0.82] | **$0.29 [$0.15–0.34]** |
| Monday model time (API) | 170 s [138–318] | **78 s [43–105]** |
| Monday tool calls (incl. sub-agent) | 21 [19–40] | 11 [9–19] |
| Monday answer named the function, 30-day limit, cut-off and delay | 2 of 3 | 3 of 3 |
| Tuesday cost | **$0.08 [$0.07–0.14]** | $0.14 [$0.12–0.15] |
| Tuesday tool calls | **3 [3–5]** | 8 [5–8] |

All six Tuesday edits are equivalent (a counter plus a `task_logger.info` call in `trigger_automatic_checkout_completion_task`).

**What happened:**
- **Monday, knock wins.** Every knock run was cheaper than every run without it. Without knock, Claude Code delegated the search to a sub-agent that made 16–39 tool calls. With knock, `map` pointed straight at the task, the settings and the scheduler, and Claude confirmed with 2–3 `open` calls.
- **Tuesday, knock loses.** Without knock, one regex search (`auto.?complet.*checkout`) found the file. Claude Code also has to read a file with its own Read tool before editing it, so knock's `map`/`open` calls were pure overhead, on top of one step to load knock's tools.
- **Duration caveat:** Claude Code's reported session duration excludes sub-agent work (13–17 s for the no-knock Monday runs). The API time above is the comparable figure.

**Caveats:** 3 runs per condition, one repository, one scenario. The map's seed-ranking bug was found and fixed while checking knock's output for these exact tasks before recording, so this scenario is not untuned. Across both days together, knock's median cost was about 24% lower ($0.43 vs $0.56); that number depends entirely on the mix of investigation and editing.

### Edit re-test after "edit mode" (3 runs each, 2026-10-31)

Edit mode gives change requests a lighter map: an exact `Read` range and real usages to follow, with no doc quotes. It was developed while looking at knock's output for these same two tasks. Only the Tuesday edits were re-run, with and without knock, in the same batch:

| | without knock | with knock (before edit mode, earlier batch) | with knock + edit mode |
|---|---|---|---|
| Saleor edit | **$0.074 [0.058–0.081] · 3 calls** | $0.144 · 8 calls | $0.097 [0.073–0.104] · 5 calls |
| Flask edit | **$0.058 [0.058–0.081] · 5 calls** | $0.085 · 7 calls | $0.073 [0.063–0.086] · 6 calls |

All 12 edits are correct and equivalent. Claude used the suggested `Read` ranges, and on Flask it read the suggested `app.py` logging lines instead of searching for them. Edit mode narrowed the gap but did not close it, and the reason is structural. Without knock, an edit whose file a single regex finds takes 3 calls (grep, Read, Edit). With knock, the minimum is 4: loading the MCP tools (a fixed Claude Code step), `map`, Read, Edit. **For small edits in code that one search can find, the agent should skip knock.** The win is in investigations: on Saleor the Monday saving ($0.20 per session) is about ten times the edit overhead ($0.02).

## Latest knock vs plain Claude Code (both repositories, 2026-10-31)

This run tests knock as on `main` at `ce7934d`: the code map, edit mode, and the guidance "use knock for exploration, skip it for small edits one grep can find". It covers the full two-day scenario on Saleor and Flask, with and without knock, 3 runs each, 24 sessions in one batch ($5.11 total). Claude got the guidance line rather than "always call knock first", and knock's memory carried over from Monday to Tuesday within each run. Medians, with ranges in brackets, as reported by Claude Code:

| | without knock | with knock |
|---|---|---|
| **Saleor, two days per run** | $0.70 [$0.40–0.80] | **$0.43 [$0.34–0.51]** |
| Saleor Monday (investigation) | $0.49 [$0.25–0.58] · 192 s API time | **$0.25 [$0.25–0.38] · 76 s** |
| Saleor Tuesday (edit) | $0.16 [$0.13–0.31] | $0.14 [$0.09–0.18] |
| **Flask, two days per run** | $0.31 [$0.23–0.41] | $0.27 [$0.25–0.47] |
| Flask Monday | $0.22 [$0.15–0.33] | $0.20 [$0.16–0.39] |
| Flask Tuesday | $0.09 [$0.08–0.09] | $0.07 [$0.07–0.09] |

- **Correctness:** all 12 Monday answers named the function, the 30-day limit (Saleor) or the fixed key ordering (Flask), and all 12 edits are equivalent. (Grading now reads all of Claude's answer text. Reading only the final message wrongly flagged one knock answer, which ended with "Saved that as a finding".)
- **Guidance followed:** Claude used knock for every Monday investigation (6/6). On Tuesday it skipped knock for all three Flask edits, and on Saleor, an unfamiliar codebase, it used knock in 2 of 3 runs.
- **Saleor:** knock's two-day cost was about 39% lower (median $0.43 vs $0.70), and each knock run cost less than the median run without it. Without knock, Claude Code delegated two of the three Monday investigations to a sub-agent (29 tool calls each).
- **Flask:** about 14% lower on the median, but the ranges overlap heavily, so this is not a clear difference. Tuesday's lower cost with knock comes from runs where knock was *not* used, so it is noise.
- **Caveats:** 3 runs per condition. The Saleor scenario is not untuned (see above). Costs vary a lot between identical runs (Saleor Tuesday without knock: $0.13–0.31).

## Model-in-the-loop benchmark (`agent_bench.py`)

This benchmark uses the same model, the same system prompt, the same 20-turn limit and the same starting state. The **baseline** gets `list_files`/`grep`/`read_file`. The **knock** condition gets the same tools plus `get_context`/`search_knowledge`/`get_knowledge`/`get_source`, served in-process by the same service the MCP server uses. The order of the two conditions alternates per task. Prompt caching is enabled identically in both conditions (top-level automatic `cache_control`).

Per run it records, from `response.usage`: `input_tokens`, `output_tokens`, `cache_read_input_tokens` and `cache_creation_input_tokens`. It also records tool calls, file reads, repeated reads of the same file, wall time, stop reasons, and pass/fail, where every required pattern must appear in the final answer. Cost is computed from measured usage and a price table in the script; check current prices before quoting it. Server-side refusal fallbacks are on by default (`--no-fallbacks` to disable). Runs that a fallback model served are flagged and excluded from the summary.

```bash
pip install -e '.[bench]'
python benchmarks/agent_bench.py --tasks benchmarks/tasks/fastapi-template.json --confirm-spend --repeats 3
```

Without `--confirm-spend` it only prints the planned number of runs.

**Status: not run.** No results are reported, because none were obtained. The harness's local tools were exercised, and its request parameters were checked against the installed `anthropic` SDK's signatures. No API request has been made with it.
