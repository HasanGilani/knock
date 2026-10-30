"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, indexer, logs
from .config import load_config
from .service import KnockError, KnockService


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if isinstance(data, dict) and "results" in data:
        for r in data["results"]:
            _item(r)
        for note in data.get("notes", []):
            print(f"note: {note}")
        print(f"freshness: {data['freshness']['state']}")
        return
    if isinstance(data, dict) and "knowledge" in data and "task" in data:
        for r in data["knowledge"]:
            _item(r)
        if data.get("symbols"):
            print("symbols:")
            for s in data["symbols"]:
                print(f"  {s['path']}:{s['line']}  {s['kind']} {s['symbol']}")
        if data.get("files_to_inspect"):
            print("files to inspect:")
            for f in data["files_to_inspect"]:
                print(f"  {f['path']}  ({f['why']})")
        if data.get("recent_changes"):
            print("recent changes:")
            for c in data["recent_changes"]:
                print(f"  {c['commit']} {c['date']} {c['subject']}")
        for g in data.get("gaps", []):
            print(f"gap: {g}")
        print(f"freshness: {data['freshness']['state']}  ({data['meta']['response_chars']} chars)")
        return
    if isinstance(data, dict) and "content" in data and "start_line" in data:
        print(data["content"])
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _item(r: dict) -> None:
    status = f" [{r['status']}]" if r.get("status") else ""
    srcs = ", ".join(s["path"] + (f":{s['lines']}" if s.get("lines") else "") for s in r.get("sources", []))
    print(f"{r['id']}  {r['category']}{status}  {r['title']}")
    print(f"    {r['content']}")
    if r.get("rationale"):
        print(f"    why: {r['rationale']}")
    print(f"    sources: {srcs}  ({r['provenance']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knock", description="Persistent, source-grounded repository knowledge for AI coding agents.")
    parser.add_argument("--version", action="version", version=f"knock {__version__}")
    parser.add_argument("--home", help="data directory (default $KNOCK_HOME or ~/.knock)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="register (if needed) and index a repository incrementally")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--full", action="store_true", help="re-extract every file")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("repos", help="list registered repositories")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("forget", help="delete all knock data for a repository (the repository itself is untouched)")
    p.add_argument("repo")
    p.add_argument("--yes", action="store_true", help="confirm deletion")

    for name, help_ in (("status", "index freshness and counts"), ("metrics", "local usage, cache and cost-related metrics")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--repo", help="repository name, id or path (default: current directory)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("search", help="search knowledge")
    p.add_argument("query")
    p.add_argument("--repo")
    p.add_argument("--category", action="append")
    p.add_argument("--path-prefix")
    p.add_argument("--all", action="store_true", help="include superseded/invalidated items")
    p.add_argument("--limit", type=int)
    p.add_argument("--max-chars", type=int)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("context", help="compact context package for a task")
    p.add_argument("task")
    p.add_argument("--repo")
    p.add_argument("--path", action="append")
    p.add_argument("--max-chars", type=int)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="show a knowledge item with provenance")
    p.add_argument("id")
    p.add_argument("--repo")
    p.add_argument("--evidence", action="store_true")

    p = sub.add_parser("source", help="print an indexed file excerpt")
    p.add_argument("path")
    p.add_argument("--repo")
    p.add_argument("--lines", help="e.g. 10-40")
    p.add_argument("--symbol")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("record", help="record a knowledge item")
    p.add_argument("--repo")
    p.add_argument("--category", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--title")
    p.add_argument("--rationale")
    p.add_argument("--source", action="append", default=[], help="path or path:START-END (repeatable)")
    p.add_argument("--supersedes", action="append")

    p = sub.add_parser("set-status", help="verify or invalidate a knowledge item")
    p.add_argument("id")
    p.add_argument("action", choices=["verify", "invalidate"])
    p.add_argument("--reason")
    p.add_argument("--repo")

    p = sub.add_parser("serve", help="run the MCP server over stdio")
    p.add_argument("--repo", action="append", help="repository path/name to serve (repeatable; default: current directory)")
    p.add_argument("--no-refresh", action="store_true", help="skip the incremental refresh at start-up")

    sub.add_parser("config", help="print effective configuration")

    args = parser.parse_args(argv)
    config = load_config(args.home)
    logs.configure(config.log_level if args.cmd == "serve" else os.environ.get("KNOCK_LOG_LEVEL", "WARNING"))
    svc = KnockService(config)
    try:
        return _dispatch(args, svc, config)
    except KnockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except indexer.IndexBusy as exc:
        print(f"busy: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted; the next `knock index` resumes where this stopped", file=sys.stderr)
        return 130
    finally:
        svc.close()


def _dispatch(args, svc: KnockService, config) -> int:
    cmd = args.cmd
    if cmd == "index":
        repo = svc.register(args.path)
        result = svc.refresh_repository(repo["id"], full=args.full)
        if args.json:
            _print(result, True)
        else:
            print(f"{result['repository']}: {result['status']} in {result['duration_ms']} ms at {result.get('revision') or 'no git revision'}")
            print(f"  files: {result['files_discovered']} eligible, {result['files_added']} added, {result['files_modified']} modified, "
                  f"{result['files_deleted']} deleted, {result['files_renamed']} renamed, {result['files_unchanged']} unchanged, "
                  f"{result['files_skipped_discovery']} excluded, {result['files_skipped_content']} skipped (binary/secret), {result['files_error']} errors")
            print(f"  knowledge: {result['knowledge_created']} created, {result['knowledge_updated']} updated, "
                  f"{result['knowledge_deduplicated']} deduplicated, {result['knowledge_invalidated']} invalidated, "
                  f"{result['agent_items_marked_stale']} agent items marked stale; {result['commits_ingested']} commits; 0 model calls")
            if result["secrets_redacted"]:
                print(f"  {result['secrets_redacted']} likely secrets redacted before extraction")
        return 0 if result["status"] == "completed" else 3
    if cmd == "repos":
        _print(svc.list_repositories(), True)
        return 0
    if cmd == "forget":
        repo = svc.resolve(args.repo)
        if not args.yes:
            print(f"This deletes all knock data for {repo.name} ({repo.root}). Re-run with --yes to confirm.", file=sys.stderr)
            return 1
        indexer.unregister(svc.conn, repo.id)
        print(f"forgot {repo.name}")
        return 0
    if cmd == "status":
        _print(svc.repository_status(args.repo), True)
        return 0
    if cmd == "metrics":
        _print(svc.get_metrics(args.repo), True)
        return 0
    if cmd == "search":
        _print(svc.search_knowledge(args.query, repository=args.repo, categories=args.category, path_prefix=args.path_prefix,
                                    include_inactive=args.all, limit=args.limit, max_chars=args.max_chars), args.json)
        return 0
    if cmd == "context":
        _print(svc.get_context(args.task, repository=args.repo, paths=args.path, max_chars=args.max_chars), args.json)
        return 0
    if cmd == "show":
        _print(svc.get_knowledge(args.id, repository=args.repo, include_evidence=args.evidence), True)
        return 0
    if cmd == "source":
        start = end = None
        if args.lines:
            a, _, b = args.lines.partition("-")
            start, end = int(a), int(b or a)
        _print(svc.get_source(args.path, repository=args.repo, start_line=start, end_line=end, symbol=args.symbol), args.json)
        return 0
    if cmd == "record":
        sources = []
        for s in args.source:
            path, _, rng = s.partition(":")
            src: dict = {"path": path}
            if rng:
                a, _, b = rng.partition("-")
                src["start_line"], src["end_line"] = int(a), int(b or a)
            sources.append(src)
        _print(svc.record_knowledge(args.category, args.content, repository=args.repo, title=args.title, rationale=args.rationale,
                                    sources=sources, supersedes=args.supersedes), True)
        return 0
    if cmd == "set-status":
        _print(svc.update_knowledge_status(args.id, args.action, reason=args.reason, repository=args.repo), True)
        return 0
    if cmd == "serve":
        from .mcp_server import serve

        ids = []
        for ref in args.repo or ["."]:
            repo = indexer.find_repo(svc.conn, ref) if not os.path.isdir(ref) else None
            ids.append(repo.id if repo else svc.register(ref)["id"])
        svc.close()
        serve(config, ids, refresh_on_start=not args.no_refresh)
        return 0
    if cmd == "config":
        _print(config.to_dict(), True)
        return 0
    raise KnockError(f"unknown command {cmd}")


if __name__ == "__main__":
    sys.exit(main())
