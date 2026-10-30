# Retrieval benchmark: full-stack-fastapi-template 0.9.0 (e4022a9502a6)

Task set role: held-out set: tasks were written and checked against the repository before knock was run on it, and knock was not changed after seeing these results.

knock 0.1.0, Python 3.10.0, macOS-27.0.1-arm64-arm-64bit. Deterministic; no model calls. Characters are measured; tokens are estimated as characters/4.

## Ingestion

- Cold index: 310 ms for 196 files (235 knowledge items, 653 symbols, 5.3 MB database).
- No-op refresh: 30 ms, 0 files reprocessed.

## Summary

| workflow | tasks passed | total chars consumed | median chars/task | est. tokens | tool calls |
|---|---|---|---|---|---|
| baseline A (grep + read 3 files) | 9/12 | 1,215,570 | 117,924 | 303,893 | 72 |
| baseline B (targeted grep + 2 windows) | 7/12 | 328,206 | 27,086 | 82,052 | 60 |
| knock (get_context + ≤2 get_source) | 10/12 | 122,506 | 7,612 | 30,627 | 17 |
| oracle (smallest single sufficient file) | — | 160,947 | — | 40,237 | — |

## Per task

| task | type | A pass | A chars | B pass | B chars | knock pass | knock chars | knock calls | missing (knock) |
|---|---|---|---|---|---|---|---|---|---|
| arch-stack | architecture | yes | 122,639 | yes | 41,375 | yes | 7,772 | 1 | — |
| arch-routers | architecture | no | 118,382 | no | 37,859 | no | 16,554 | 3 | backend/app/api/main\.py, include_router|api_router |
| locate-password-recovery | locate-feature | yes | 113,300 | yes | 30,364 | yes | 7,295 | 1 | — |
| locate-jwt | locate-feature | no | 133,137 | no | 27,074 | yes | 7,559 | 1 | — |
| deploy-production | deployment | yes | 119,092 | yes | 27,270 | yes | 7,265 | 1 | — |
| workflow-backend-tests | deployment | yes | 117,465 | yes | 46,127 | yes | 7,273 | 1 | — |
| bug-container-stops | bug | yes | 119,994 | yes | 21,594 | yes | 16,285 | 2 | — |
| bug-first-start | bug | no | 14,116 | no | 7,151 | yes | 7,624 | 1 | — |
| why-compose-file | rationale | yes | 124,626 | yes | 27,097 | yes | 7,599 | 1 | — |
| bug-cors-parsing | bug | yes | 113,648 | yes | 26,198 | yes | 7,731 | 1 | — |
| convention-lint | modify-with-conventions | yes | 105,929 | no | 25,189 | no | 22,078 | 3 | ruff, mypy, biome |
| modify-migration | modify-with-conventions | yes | 13,242 | no | 10,908 | yes | 7,471 | 1 | — |

## Cache (application-level)

- First get_context median 36.75 ms; repeated identical request median 22.09 ms (12/12 served from cache).
- Provider-side prompt caching is not involved in this benchmark.

## Staleness scenario

- backend/README.md: 'alembic upgrade head' -> 'alembic migrate --to-latest' (not yet re-indexed)
- Before refresh: outdated text served = True; flagged stale = True.
- Refresh: 1 file reprocessed, 195 unchanged, 35 ms.
- After refresh: new text served = True; old text served = False.
