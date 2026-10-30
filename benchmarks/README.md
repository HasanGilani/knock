# Benchmarks

- `retrieval_bench.py`: deterministic retrieval benchmark (no API needed). Results are in `results/`.
- `agent_bench.py`: optional model-in-the-loop benchmark (needs Anthropic credentials and spends money; requires `--confirm-spend`).
- `tasks/`: pinned repositories and tasks (`flask.json` = development set, `fastapi-template.json` = held-out set).

Methodology, results and threats to validity: [../docs/benchmark.md](../docs/benchmark.md).
