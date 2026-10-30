# Retrieval benchmark: flask 3.1.2 (2c1b30d0503c)

Task set role: development set: knock's extractors and ranking were adjusted while looking at results on this repository, so these results are optimistic.

knock 0.1.0, Python 3.10.0, macOS-27.0.1-arm64-arm-64bit. Deterministic; no model calls. Characters are measured; tokens are estimated as characters/4.

## Ingestion

- Cold index: 579 ms for 196 files (548 knowledge items, 1265 symbols, 6.3 MB database).
- No-op refresh: 30 ms, 0 files reprocessed.

## Summary

| workflow | tasks passed | total chars consumed | median chars/task | est. tokens | tool calls |
|---|---|---|---|---|---|
| baseline A (grep + read 3 files) | 7/12 | 2,141,848 | 180,400 | 535,462 | 72 |
| baseline B (targeted grep + 2 windows) | 7/12 | 246,604 | 21,842 | 61,651 | 60 |
| knock (get_context + ≤2 get_source) | 12/12 | 107,164 | 7,626 | 26,791 | 14 |
| oracle (smallest single sufficient file) | — | 244,044 | — | 61,011 | — |

## Per task

| task | type | A pass | A chars | B pass | B chars | knock pass | knock chars | knock calls | missing (knock) |
|---|---|---|---|---|---|---|---|---|---|
| arch-contexts | architecture | yes | 156,618 | no | 23,170 | yes | 7,289 | 1 | — |
| arch-dispatch | architecture | yes | 210,209 | yes | 21,728 | yes | 7,623 | 1 | — |
| locate-blueprint-register | locate-feature | yes | 154,866 | yes | 21,957 | yes | 7,239 | 1 | — |
| locate-default-config | locate-feature | yes | 155,111 | no | 24,916 | yes | 7,814 | 1 | — |
| deploy-gunicorn | deployment | no | 180,667 | yes | 23,431 | yes | 7,630 | 1 | — |
| workflow-tests | deployment | yes | 280,970 | yes | 23,020 | yes | 7,259 | 1 | — |
| bug-ide-debugger | bug | yes | 118,607 | yes | 14,405 | yes | 7,318 | 1 | — |
| bug-secret-key-fallbacks | bug | yes | 209,401 | yes | 11,474 | yes | 7,914 | 1 | — |
| why-explicit-app | rationale | no | 180,132 | yes | 19,311 | yes | 18,781 | 2 | — |
| why-context-locals | rationale | no | 183,470 | no | 20,037 | yes | 7,198 | 1 | — |
| convention-new-config | modify-with-conventions | no | 298,878 | no | 25,113 | yes | 7,651 | 1 | — |
| convention-imports | modify-with-conventions | no | 12,919 | no | 18,042 | yes | 13,448 | 2 | — |

## Cache (application-level)

- First get_context median 63.7 ms; repeated identical request median 23.15 ms (12/12 served from cache).
- Provider-side prompt caching is not involved in this benchmark.

## Staleness scenario

- docs/deploying/gunicorn.rst: 'gunicorn -w 4' -> 'gunicorn --workers 8' (not yet re-indexed)
- Before refresh: outdated text served = True; flagged stale = True.
- Refresh: 1 file reprocessed, 195 unchanged, 36 ms.
- After refresh: new text served = True; old text served = False.
