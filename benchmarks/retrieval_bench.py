"""Deterministic retrieval benchmark: grep-and-read baseline vs. knock, no model API needed.

Both workflows receive the same task text and the same term extraction. The question
measured is: how much text must a workflow consume before the facts needed for the
task are in front of the agent, and how many tool calls does it take?

Baseline A, "grep + read" (models a typical agent without a knowledge layer):
  1. One `grep -il` (files-with-matches) per key term: the 3 rarest task terms present in the repo.
  2. Read, in full (Read-tool style, line-numbered, max 2000 lines), the top 3 files ranked by
     number of distinct terms matched, then total matches.
Baseline B, "targeted grep" (a frugal agent):
  1. One `grep -in` (matching lines, capped at 60 lines) per key term, same 3 terms.
  2. Read a 120-line window around the densest cluster of matches in each of the top 2 files.
Knock:
  1. One get_context(task) call (default budget).
  2. If facts are still missing: get_source on the top `files_to_inspect` entries (max 2 calls).
Oracle (lower bound, not a workflow): the smallest single file that contains every required fact.

A task passes when every required regex appears in the consumed text. This is a retrieval
proxy: it says whether the information reached the agent, not whether an agent would use it.

Usage:
  python benchmarks/retrieval_bench.py [--tasks benchmarks/tasks/flask.json] [--out benchmarks/results]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from knock import __version__, indexer  # noqa: E402
from knock.config import Config  # noqa: E402
from knock.service import KnockService  # noqa: E402
from knock.util import estimate_tokens, terms  # noqa: E402

READ_MAX_LINES = 2000
BASELINE_TERMS = 3
BASELINE_READS = 3
KNOCK_FOLLOWUPS = 2
GREP_LINE_CAP = 60
TARGETED_READS = 2
WINDOW = 120


def ensure_checkout(spec: dict, cache: Path) -> Path:
    dest = cache / f"{spec['name']}-{spec['commit'][:12]}"
    if not dest.exists():
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "-q", "--depth", str(spec.get("history_depth", 200)),
                        "--branch", spec["ref"], spec["url"], str(dest)], check=True)
    head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if head != spec["commit"]:
        raise SystemExit(f"checkout {dest} is at {head}, expected {spec['commit']}")
    return dest


def repo_text_files(root: Path) -> dict[str, str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout.decode()
    files = {}
    for rel in filter(None, out.split("\0")):
        p = root / rel
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192] or len(data) > 1_000_000:
            continue
        files[rel] = data.decode("utf-8", "replace")
    return files


def as_read(text: str) -> str:
    lines = text.splitlines()[:READ_MAX_LINES]
    return "\n".join(f"{i}\t{line}" for i, line in enumerate(lines, 1))


def passes(required: list[str], consumed: str) -> tuple[bool, list[str]]:
    missing = [r for r in required if not re.search(r, consumed, re.I)]
    return not missing, missing


# ----------------------------------------------------------------- baseline
def baseline(task: dict, files: dict[str, str], lowered: dict[str, str]) -> dict:
    t0 = time.perf_counter()
    qterms = terms(task["prompt"])
    df = {t: sum(1 for txt in lowered.values() if t in txt) for t in qterms}
    present = [t for t in qterms if df[t] > 0]
    chosen = sorted(present, key=lambda t: (df[t], qterms.index(t)))[:BASELINE_TERMS]
    consumed_parts, calls, matched = [], 0, {}
    for t in chosen:
        hits = [p for p, txt in lowered.items() if t in txt]
        calls += 1
        consumed_parts.append("\n".join(hits))           # files_with_matches output
        for p in hits:
            m = matched.setdefault(p, [set(), 0])
            m[0].add(t)
            m[1] += lowered[p].count(t)
    ranked = sorted(matched, key=lambda p: (-len(matched[p][0]), -matched[p][1], p))[:BASELINE_READS]
    for p in ranked:
        calls += 1
        consumed_parts.append(f"==> {p}\n" + as_read(files[p]))
    consumed = "\n".join(consumed_parts)
    ok, missing = passes(task["required"], consumed)
    return {"pass": ok, "missing": missing, "chars": len(consumed), "tool_calls": calls, "files_read": ranked,
            "terms": chosen, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}


def baseline_targeted(task: dict, files: dict[str, str], lowered: dict[str, str]) -> dict:
    t0 = time.perf_counter()
    qterms = terms(task["prompt"])
    df = {t: sum(1 for txt in lowered.values() if t in txt) for t in qterms}
    chosen = sorted([t for t in qterms if df[t] > 0], key=lambda t: (df[t], qterms.index(t)))[:BASELINE_TERMS]
    consumed_parts, calls = [], 0
    hits_by_file: dict[str, dict[int, set[str]]] = {}
    for t in chosen:
        calls += 1
        out = []
        for p in sorted(lowered):
            for n, line in enumerate(lowered[p].splitlines(), 1):
                if t in line:
                    hits_by_file.setdefault(p, {}).setdefault(n, set()).add(t)
                    if len(out) < GREP_LINE_CAP:
                        out.append(f"{p}:{n}:{files[p].splitlines()[n - 1][:200]}")
        consumed_parts.append("\n".join(out))
    def file_rank(p: str):
        termset = set().union(*hits_by_file[p].values())
        return (-len(termset), -len(hits_by_file[p]), p)
    ranked = sorted(hits_by_file, key=file_rank)[:TARGETED_READS]
    for p in ranked:
        lines = sorted(hits_by_file[p])
        # Window around the densest cluster of matching lines.
        best = max(lines, key=lambda n: sum(1 for m in lines if abs(m - n) <= WINDOW // 2))
        start = max(1, best - WINDOW // 2)
        text_lines = files[p].splitlines()[start - 1 : start - 1 + WINDOW]
        calls += 1
        consumed_parts.append(f"==> {p}\n" + "\n".join(f"{start + i}\t{line}" for i, line in enumerate(text_lines)))
    consumed = "\n".join(consumed_parts)
    ok, missing = passes(task["required"], consumed)
    return {"pass": ok, "missing": missing, "chars": len(consumed), "tool_calls": calls, "files_read": ranked,
            "terms": chosen, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}


def oracle(task: dict, files: dict[str, str]) -> dict:
    best = None
    for p, txt in files.items():
        if all(re.search(r, txt, re.I) or re.search(r, p, re.I) for r in task["required"]):
            size = len(as_read(txt))
            if best is None or size < best[1]:
                best = (p, size)
    return {"file": best[0], "chars": best[1]} if best else {"file": None, "chars": None}


# -------------------------------------------------------------------- knock
def knock_flow(task: dict, svc: KnockService, repo: str) -> dict:
    t0 = time.perf_counter()
    ctx = svc.get_context(task["prompt"], repository=repo)
    text = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
    consumed, calls = text, 1
    ok, missing = passes(task["required"], consumed)
    followed = []
    for f in ctx.get("files_to_inspect", [])[:KNOCK_FOLLOWUPS]:
        if ok:
            break
        src = svc.get_source(f["path"], repository=repo)
        consumed += "\n" + json.dumps(src, ensure_ascii=False, separators=(",", ":"))
        calls += 1
        followed.append(f["path"])
        ok, missing = passes(task["required"], consumed)
    return {"pass": ok, "missing": missing, "chars": len(consumed), "tool_calls": calls, "context_chars": len(text),
            "followed_sources": followed, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "freshness": ctx["freshness"]["state"]}


def knock_map_flow(task: dict, svc: KnockService, repo: str) -> dict:
    """knock v2: map(task), then open() the suggested symbols (max 2) while facts are missing."""
    t0 = time.perf_counter()
    text = svc.code_map(task["prompt"], repository=repo)
    consumed, calls = text, 1
    ok, missing = passes(task["required"], consumed)
    followed = []
    for target in re.findall(r'open\("([^"]+)"\)', text)[:KNOCK_FOLLOWUPS]:
        if ok or target == "<name>":
            continue
        consumed += "\n" + svc.open_code(target, repository=repo)
        calls += 1
        followed.append(target)
        ok, missing = passes(task["required"], consumed)
    return {"pass": ok, "missing": missing, "chars": len(consumed), "tool_calls": calls, "map_chars": len(text),
            "followed": followed, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}


# ------------------------------------------------------------- staleness
def staleness_scenario(src_root: Path, workdir: Path, config: Config, sc: dict) -> dict:
    """Change a documented fact in a copy of the repo and check knock's behaviour before and after refresh."""
    root = workdir / "stale-copy"
    shutil.copytree(src_root, root, symlinks=True)
    svc = KnockService(config)
    rid = svc.register(str(root))["id"]
    svc.refresh_repository(rid)
    doc = root / sc["file"]
    doc.write_text(doc.read_text().replace(sc["old"], sc["new"]))
    indexer.reset_freshness_cache()
    before = svc.get_context(sc["query"], repository=rid)
    blob = json.dumps(before, ensure_ascii=False)
    flagged = before["freshness"]["state"] == "stale" and "file modified in working tree since last index" in blob
    served_old = sc["old"] in blob
    refresh = svc.refresh_repository(rid)
    after = json.dumps(svc.get_context(sc["query"], repository=rid), ensure_ascii=False)
    svc.close()
    return {
        "edit": f"{sc['file']}: '{sc['old']}' -> '{sc['new']}' (not yet re-indexed)",
        "before_refresh": {"served_outdated_text": served_old, "flagged_as_stale": flagged, "freshness": before["freshness"]},
        "refresh": {k: refresh[k] for k in ("files_modified", "files_unchanged", "knowledge_updated", "knowledge_invalidated", "duration_ms")},
        "after_refresh": {"serves_new_text": sc["new"] in after, "serves_old_text": sc["old"] in after},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", default=str(HERE / "tasks" / "flask.json"))
    ap.add_argument("--cache", default=str(HERE / ".cache"))
    ap.add_argument("--out", default=str(HERE / "results"))
    args = ap.parse_args()
    spec = json.loads(Path(args.tasks).read_text())
    root = ensure_checkout(spec["repository"], Path(args.cache))

    with tempfile.TemporaryDirectory() as tmp:
        config = Config(home=Path(tmp) / "home", freshness_ttl_seconds=0.0)
        svc = KnockService(config)
        rid = svc.register(str(root))["id"]
        t = time.perf_counter()
        cold = svc.refresh_repository(rid)
        cold_ms = (time.perf_counter() - t) * 1000
        t = time.perf_counter()
        warm = svc.refresh_repository(rid)
        warm_ms = (time.perf_counter() - t) * 1000
        status = svc.repository_status(rid)
        db_bytes = sum(f.stat().st_size for f in config.home.glob("knock.db*"))

        files = repo_text_files(root)
        lowered = {p: t.lower() for p, t in files.items()}
        rows = []
        for task in spec["tasks"]:
            b = baseline(task, files, lowered)
            bt = baseline_targeted(task, files, lowered)
            k = knock_flow(task, svc, rid)
            km = knock_map_flow(task, svc, rid)
            o = oracle(task, files)
            rows.append({"id": task["id"], "type": task["type"], "baseline": b, "baseline_targeted": bt, "knock": k,
                         "knock_map": km, "oracle": o})
        # Cache behaviour: the same context request again (application-level cache only).
        cache_timings = []
        for task in spec["tasks"]:
            t = time.perf_counter()
            r = svc.get_context(task["prompt"], repository=rid)
            cache_timings.append(((time.perf_counter() - t) * 1000, r["meta"]["cached"]))
        metrics = svc.get_metrics(rid)
        svc.close()
        stale = staleness_scenario(root, Path(tmp), Config(home=Path(tmp) / "home2", freshness_ttl_seconds=0.0), spec["staleness"])

    def agg(side: str) -> dict:
        chars = [r[side]["chars"] for r in rows]
        return {
            "passed": sum(r[side]["pass"] for r in rows),
            "tasks": len(rows),
            "total_chars": sum(chars),
            "median_chars": statistics.median(chars),
            "est_total_tokens": estimate_tokens(sum(chars)),
            "total_tool_calls": sum(r[side]["tool_calls"] for r in rows),
        }

    summary = {
        "knock_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "repository": spec["repository"],
        "task_set_role": spec.get("role", "unspecified"),
        "ingestion": {
            "cold_index_ms": round(cold_ms), "noop_refresh_ms": round(warm_ms),
            "files_indexed": cold["files_added"], "files_reprocessed_on_noop": warm["files_added"] + warm["files_modified"],
            "knowledge_items_active": sum(v for k, v in status["counts"]["knowledge_by_status"].items() if k in ("active", "stale")),
            "symbols": status["counts"]["symbols"], "database_bytes": db_bytes, "model_calls": 0,
        },
        "baseline": agg("baseline"),
        "baseline_targeted": agg("baseline_targeted"),
        "knock": agg("knock"),
        "knock_map": agg("knock_map"),
        "oracle_total_chars": sum(r["oracle"]["chars"] or 0 for r in rows),
        "cache": {
            "repeat_context_ms_median": round(statistics.median(t for t, _ in cache_timings), 2),
            "first_context_ms_median": round(statistics.median(r["knock"]["latency_ms"] for r in rows), 2),
            "repeat_hits": sum(1 for _, hit in cache_timings if hit),
            "metrics": metrics["cache"],
        },
        "staleness_scenario": stale,
        "tasks": rows,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"retrieval_{spec['repository']['name']}"
    (out / f"{stem}.json").write_text(json.dumps(summary, indent=2))
    (out / f"{stem}.md").write_text(render(summary))
    print(render(summary))


def render(s: dict) -> str:
    b, bt, k, km = s["baseline"], s["baseline_targeted"], s["knock"], s["knock_map"]
    lines = [
        f"# Retrieval benchmark: {s['repository']['name']} {s['repository']['ref']} ({s['repository']['commit'][:12]})",
        "",
        f"Task set role: {s['task_set_role']}.",
        "",
        f"knock {s['knock_version']}, Python {s['python']}, {s['platform']}. Deterministic; no model calls. "
        "Characters are measured; tokens are estimated as characters/4.",
        "",
        "## Ingestion",
        "",
        f"- Cold index: {s['ingestion']['cold_index_ms']} ms for {s['ingestion']['files_indexed']} files "
        f"({s['ingestion']['knowledge_items_active']} knowledge items, {s['ingestion']['symbols']} symbols, "
        f"{s['ingestion']['database_bytes'] / 1e6:.1f} MB database).",
        f"- No-op refresh: {s['ingestion']['noop_refresh_ms']} ms, {s['ingestion']['files_reprocessed_on_noop']} files reprocessed.",
        "",
        "## Summary",
        "",
        "| workflow | tasks passed | total chars consumed | median chars/task | est. tokens | tool calls |",
        "|---|---|---|---|---|---|",
        f"| baseline A (grep + read 3 files) | {b['passed']}/{b['tasks']} | {b['total_chars']:,} | {b['median_chars']:,.0f} | {b['est_total_tokens']:,} | {b['total_tool_calls']} |",
        f"| baseline B (targeted grep + 2 windows) | {bt['passed']}/{bt['tasks']} | {bt['total_chars']:,} | {bt['median_chars']:,.0f} | {bt['est_total_tokens']:,} | {bt['total_tool_calls']} |",
        f"| knock (get_context + ≤2 get_source) | {k['passed']}/{k['tasks']} | {k['total_chars']:,} | {k['median_chars']:,.0f} | {k['est_total_tokens']:,} | {k['total_tool_calls']} |",
        f"| knock v2 map (map + ≤2 open) | {km['passed']}/{km['tasks']} | {km['total_chars']:,} | {km['median_chars']:,.0f} | {km['est_total_tokens']:,} | {km['total_tool_calls']} |",
        f"| oracle (smallest single sufficient file) | — | {s['oracle_total_chars']:,} | — | {estimate_tokens(s['oracle_total_chars']):,} | — |",
        "",
        "## Per task",
        "",
        "| task | type | A pass | A chars | B pass | B chars | knock pass | knock chars | v2 pass | v2 chars | v2 calls | missing (v2) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in s["tasks"]:
        lines.append(
            f"| {r['id']} | {r['type']} | {'yes' if r['baseline']['pass'] else 'no'} | {r['baseline']['chars']:,} | "
            f"{'yes' if r['baseline_targeted']['pass'] else 'no'} | {r['baseline_targeted']['chars']:,} | "
            f"{'yes' if r['knock']['pass'] else 'no'} | {r['knock']['chars']:,} | "
            f"{'yes' if r['knock_map']['pass'] else 'no'} | {r['knock_map']['chars']:,} | {r['knock_map']['tool_calls']} | "
            f"{', '.join(r['knock_map']['missing']) or '—'} |"
        )
    c = s["cache"]
    st = s["staleness_scenario"]
    lines += [
        "",
        "## Cache (application-level)",
        "",
        f"- First get_context median {c['first_context_ms_median']} ms; repeated identical request median "
        f"{c['repeat_context_ms_median']} ms ({c['repeat_hits']}/{len(s['tasks'])} served from cache).",
        "- Provider-side prompt caching is not involved in this benchmark.",
        "",
        "## Staleness scenario",
        "",
        f"- {st['edit']}",
        f"- Before refresh: outdated text served = {st['before_refresh']['served_outdated_text']}; flagged stale = {st['before_refresh']['flagged_as_stale']}.",
        f"- Refresh: {st['refresh']['files_modified']} file reprocessed, {st['refresh']['files_unchanged']} unchanged, {st['refresh']['duration_ms']} ms.",
        f"- After refresh: new text served = {st['after_refresh']['serves_new_text']}; old text served = {st['after_refresh']['serves_old_text']}.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
