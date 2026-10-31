# Retrieval benchmark: full-stack-fastapi-template 0.9.0 (e4022a9502a6)

Task set role: held-out set: tasks were written and checked against the repository before knock was run on it, and knock was not changed after seeing these results.

knock 0.1.0, Python 3.10.0, macOS-27.0.1-arm64-arm-64bit. Deterministic; no model calls. Characters are measured; tokens are estimated as characters/4.

## Ingestion

- Cold index: 329 ms for 196 files (237 knowledge items, 670 symbols, 5.3 MB database).
- No-op refresh: 28 ms, 0 files reprocessed.

## Summary

| workflow | tasks passed | total chars consumed | median chars/task | est. tokens | tool calls |
|---|---|---|---|---|---|
| baseline A (grep + read 3 files) | 9/12 | 1,215,570 | 117,924 | 303,893 | 72 |
| baseline B (targeted grep + 2 windows) | 7/12 | 328,206 | 27,086 | 82,052 | 60 |
| knock (get_context + ≤2 get_source) | 11/12 | 79,662 | 4,333 | 19,916 | 15 |
| knock v2 map (map + ≤2 open) | 9/12 | 30,252 | 2,302 | 7,563 | 15 |
| oracle (smallest single sufficient file) | — | 160,947 | — | 40,237 | — |

## Per task

| task | type | A pass | A chars | B pass | B chars | knock pass | knock chars | v2 pass | v2 chars | v2 calls | missing (v2) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| arch-stack | architecture | yes | 122,639 | yes | 41,375 | yes | 3,128 | yes | 2,148 | 1 | — |
| arch-routers | architecture | no | 118,382 | no | 37,859 | yes | 5,885 | yes | 1,620 | 1 | — |
| locate-password-recovery | locate-feature | yes | 113,300 | yes | 30,364 | yes | 4,033 | yes | 2,279 | 1 | — |
| locate-jwt | locate-feature | no | 133,137 | no | 27,074 | yes | 5,555 | yes | 1,426 | 1 | — |
| deploy-production | deployment | yes | 119,092 | yes | 27,270 | yes | 3,867 | yes | 2,324 | 1 | — |
| workflow-backend-tests | deployment | yes | 117,465 | yes | 46,127 | yes | 7,572 | yes | 3,577 | 1 | — |
| bug-container-stops | bug | yes | 119,994 | yes | 21,594 | yes | 14,435 | no | 4,808 | 2 | syntax error |
| bug-first-start | bug | no | 14,116 | no | 7,151 | yes | 4,416 | no | 3,900 | 2 | first time, database |
| why-compose-file | rationale | yes | 124,626 | yes | 27,097 | yes | 1,547 | yes | 550 | 1 | — |
| bug-cors-parsing | bug | yes | 113,648 | yes | 26,198 | yes | 4,250 | yes | 672 | 1 | — |
| convention-lint | modify-with-conventions | yes | 105,929 | no | 25,189 | no | 21,755 | no | 4,399 | 2 | ruff, mypy, biome |
| modify-migration | modify-with-conventions | yes | 13,242 | no | 10,908 | yes | 3,219 | yes | 2,549 | 1 | — |

## Cache (application-level)

- First get_context median 39.75 ms; repeated identical request median 21.37 ms (12/12 served from cache).
- Provider-side prompt caching is not involved in this benchmark.

## Staleness scenario

- backend/README.md: 'alembic upgrade head' -> 'alembic migrate --to-latest' (not yet re-indexed)
- Before refresh: outdated text served = True; flagged stale = True.
- Refresh: 1 file reprocessed, 195 unchanged, 34 ms.
- After refresh: new text served = True; old text served = False.
