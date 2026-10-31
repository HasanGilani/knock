"""Code map: a compact, structural view of the code relevant to a task.

Instead of notes *about* code, the map shows the code's own structure with the noise removed:
signatures, line ranges, a one-line purpose, what each function uses and catches, and who calls
it. It is ranked for the task in three steps:

1. **Seeds**: symbols whose name or body references match the task's words, plus exact code
   identifiers written in the task (``SECRET_KEY_FALLBACKS``, ``open_session``), plus code that
   knowledge items (commits, decisions, agent findings) point at.
2. **Graph spread**: seeds pass part of their score to their neighbours: functions they call,
   functions that call them, and their enclosing class (a small personalized-PageRank step).
3. **Render**: the best symbols, grouped by file and class, fitted to a character budget, followed
   by the few notes code cannot express (why a change was made, what an agent found) and pointers
   to relevant documentation.

``open`` then returns exact, line-numbered code for one symbol with its neighbours' signatures.
Bodies are never paraphrased, so edits can rely on them.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from . import retrieval
from .extractors.code import is_test_path
from .knowledge import fts_query
from .util import clean_text, expand_terms, term_match, terms

MAP_MAX_CHARS = 3_000
MIN_RELATIVE_SCORE = 0.3
EDIT_READ_MAX_LINES = 200
_USAGE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*[.(]")  # a name that is called or has an attribute accessed
DOCS_QUOTED = 2
DOC_QUOTE_CHARS = 380
DOCS_QUOTED_DOC_QUESTION = 4
DOC_QUOTE_CHARS_DOC_QUESTION = 600
MAX_PER_CLASS = 4
NOTES_MAX_CHARS = 1_200
OPEN_MAX_LINES = 120
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_PERIPHERAL = re.compile(r"(^|/)(examples?|samples?|demos?|fixtures?|testdata|test_data|benchmarks?|docs?|\.[\w-]+)(/|$)", re.I)
SEED_POOL = 2000   # FTS candidates scored per task; large repositories need a wide pool
SEED_LIMIT = 60


def code_identifiers(text: str) -> list[str]:
    """Words in free text that look like code: snake_case, CamelCase, CONSTANTS, dotted names."""
    out: dict[str, None] = {}
    for word in _IDENT.findall(text):
        last = word.split(".")[-1]
        if len(last) < 3:
            continue
        if "_" in last or "." in word or re.search(r"[a-z][A-Z]", last) or (last.isupper() and len(last) > 3):
            out.setdefault(word, None)
    return list(out)


_CHANGE_VERBS = re.compile(
    r"^(?:please\s+|we\s+(?:need|want)\s+to\s+|i\s+(?:need|want)\s+to\s+|can\s+you\s+|let'?s\s+)?"
    r"(add|implement|change|modify|update|fix|rename|remove|delete|refactor|introduce|log|make|support|replace|move|"
    r"extract|create|write|emit|raise|handle|expose|allow|prevent|ensure|validate|reject|return|store|send|record)\b",
    re.I,
)


def _string_lines(source: str) -> set[int]:
    """Line numbers covered entirely by string literals (docstrings, long messages) in Python source."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return out


def is_change_request(task: str) -> bool:
    """True when some clause of the task is an instruction to change code ("add …", "log how many …")."""
    for clause in re.split(r"[.;:,!?\n]+|\band\b", task):
        if _CHANGE_VERBS.match(clause.strip()):
            return True
    return False


@dataclass
class Sym:
    id: int
    path: str
    name: str
    kind: str
    line: int
    end_line: int | None
    signature: str
    doc: str
    refs: list[str]
    catches: list[str]
    score: float = 0.0
    why: set[str] = field(default_factory=set)

    @property
    def short(self) -> str:
        return self.name.split(".")[-1]

    @property
    def owner(self) -> str | None:
        return self.name.split(".")[0] if "." in self.name else None


def _sym(row: sqlite3.Row) -> Sym:
    return Sym(
        id=row["rowid"], path=row["path"], name=row["name"], kind=row["kind"], line=row["line"],
        end_line=row["end_line"], signature=row["signature"] or "", doc=row["doc"] or "",
        refs=json.loads(row["refs"]) if row["refs"] else [], catches=json.loads(row["catches"]) if row["catches"] else [],
    )


class CodeMap:
    def __init__(self, conn: sqlite3.Connection, repo_id: str):
        self.conn = conn
        self.repo_id = repo_id
        self._cache: dict[int, Sym] = {}

    # ------------------------------------------------------------ lookups
    def _rows(self, where: str, params: tuple) -> list[Sym]:
        rows = self.conn.execute(
            f"SELECT rowid, * FROM symbols WHERE repo_id=? AND {where}", (self.repo_id, *params)).fetchall()
        out = []
        for r in rows:
            s = self._cache.get(r["rowid"]) or _sym(r)
            self._cache[s.id] = s
            out.append(s)
        return out

    def by_name(self, name: str) -> list[Sym]:
        """Exact qualified name, or any symbol whose last name component is ``name``."""
        if "." in name:
            exact = self._rows("name = ?", (name,))
            if exact:
                return exact
            name = name.split(".")[-1]
        like = "%." + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self._rows("(name = ? OR name LIKE ? ESCAPE '\\')", (name, like))

    def callees(self, s: Sym, limit: int = 8) -> list[Sym]:
        """Repository symbols this symbol references, resolved by name (ambiguous common names skipped)."""
        out: list[Sym] = []
        for ref in s.refs:
            if len(out) >= limit:
                break
            cands = [c for c in self.by_name(ref) if c.id != s.id and c.kind not in ("attribute",)]
            if not cands:
                continue
            same_scope = [c for c in cands if c.path == s.path and (c.owner == s.owner or c.owner is None)]
            if same_scope:
                cands = same_scope
            if len(cands) > 3:
                continue  # too generic to resolve statically ("get", "run", ...)
            out.extend(c for c in cands[:2] if c not in out)
        return out[:limit]

    def callers(self, s: Sym, limit: int = 6) -> list[Sym]:
        """Symbols whose body references this symbol's name."""
        q = fts_query([s.short])
        if not q:
            return []
        rows = self.conn.execute(
            "SELECT path, name, line FROM symbols_fts WHERE symbols_fts MATCH ? AND repo_id=? LIMIT 200",
            (f"refs : ({q})", self.repo_id),
        ).fetchall()
        out: list[Sym] = []
        for r in rows:
            for c in self._rows("path=? AND name=? AND line=?", (r["path"], r["name"], r["line"])):
                if c.id != s.id and s.short in c.refs and c not in out:
                    out.append(c)
        # Callers in the same file first, then non-test code, then tests.
        out.sort(key=lambda c: (c.path != s.path, is_test_path(c.path), c.path, c.line))
        return out[:limit]

    # ------------------------------------------------------------ ranking
    def rank(self, task: str, knowledge: list[retrieval.Ranked], limit: int = 12) -> list[Sym]:
        mentions_tests = bool(re.search(r"\btests?\b", task, re.I))
        scores: dict[int, Sym] = {}

        def bump(s: Sym, amount: float, reason: str) -> None:
            cur = scores.setdefault(s.id, s)
            cur.score += amount
            cur.why.add(reason)

        candidates = retrieval.score_symbols(self.conn, self.repo_id, task, pool=SEED_POOL)
        candidates.sort(key=lambda x: (-x[0], x[1]["path"], x[1]["line"]))  # best first, not FTS order
        for score, r in candidates[:SEED_LIMIT]:
            for s in self._rows("path=? AND name=? AND line=?", (r["path"], r["name"], r["line"])):
                bump(s, score, "matches task")
        for ident in code_identifiers(task):
            for s in self.by_name(ident)[:5]:
                bump(s, 4.0, f"named in task ({ident})")
            q = fts_query([ident.split(".")[-1]])
            if q:
                for r in self.conn.execute(
                        "SELECT path, name, line FROM symbols_fts WHERE symbols_fts MATCH ? AND repo_id=? LIMIT 40",
                        (f"refs : ({q})", self.repo_id)):
                    for s in self._rows("path=? AND name=? AND line=?", (r["path"], r["name"], r["line"])):
                        if ident.split(".")[-1] in s.refs:
                            bump(s, 2.5, f"uses {ident.split('.')[-1]}")
        # Code that knowledge points at (cited line ranges) gets a share of the item's relevance.
        for rank_i, item in enumerate(knowledge[:6]):
            weight = 1.5 / (1 + rank_i)
            for src in item.sources:
                if not src.get("start_line") or src["path"] == ".":
                    continue
                for s in self._rows("path=? AND line<=? AND COALESCE(end_line, line)>=?",
                                    (src["path"], src.get("end_line") or src["start_line"], src["start_line"])):
                    bump(s, weight, "cited by knowledge")
        # Graph spread from the strongest seeds.
        seeds = sorted(scores.values(), key=lambda x: -x.score)[:12]
        for seed in seeds:
            share = 0.35 * seed.score
            for c in self.callees(seed, limit=6):
                bump(c, share * 0.8, f"called by {seed.short}")
            for c in self.callers(seed, limit=4):
                bump(c, share * 0.6, f"calls {seed.short}")
            if seed.owner:
                for parent in self._rows("path=? AND name=? AND kind='class'", (seed.path, seed.owner)):
                    bump(parent, share * 0.5, "encloses a match")
        ranked = []
        for s in scores.values():
            mult = 1.0
            if not mentions_tests and is_test_path(s.path):
                mult *= 0.3
            if _PERIPHERAL.search(s.path):
                mult *= 0.6
            if s.kind in ("attribute",):
                mult *= 0.4
            s.score *= mult
            ranked.append(s)
        ranked.sort(key=lambda x: (-x.score, x.path, x.line))
        if not ranked:
            return []
        # Keep what is clearly relevant, and at most a few members of any one class.
        floor = ranked[0].score * MIN_RELATIVE_SCORE
        out: list[Sym] = []
        per_owner: dict[tuple[str, str | None], int] = {}
        for s in ranked:
            if s.score < floor:
                break
            key = (s.path, s.owner)
            if s.owner and per_owner.get(key, 0) >= MAX_PER_CLASS:
                continue
            per_owner[key] = per_owner.get(key, 0) + 1
            out.append(s)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------- render
    def render(self, task: str, header: str, ranked: list[Sym], knowledge: list[retrieval.Ranked],
               max_chars: int = MAP_MAX_CHARS, edit: bool = False,
               read_lines: Callable[[str], list[str]] | None = None) -> tuple[str, list[str]]:
        """Return (text, open_targets). ``edit`` gives the lighter map used for change requests."""
        qterms = set(terms(task))
        by_file: dict[str, list[Sym]] = {}
        for s in ranked:
            by_file.setdefault(s.path, []).append(s)
        lines = [header, ""]
        used = sum(len(x) + 1 for x in lines)
        shown: list[Sym] = []
        for path, syms in by_file.items():
            block = [path]
            owners_done: set[str] = set()
            for s in sorted(syms, key=lambda x: x.line):
                if s.owner and s.owner not in owners_done and not any(x.name == s.owner for x in syms):
                    for parent in self._rows("path=? AND name=? AND kind='class'", (path, s.owner))[:1]:
                        block.append(f"  {parent.signature}  L{parent.line}-{parent.end_line or '?'}" + (f" — {clean_text(parent.doc, 90)}" if parent.doc else ""))
                    owners_done.add(s.owner)
                block.extend(self._render_symbol(s, qterms, indent="    " if s.owner else "  "))
            text = "\n".join(block)
            if used + len(text) + 1 > max_chars and shown:
                break
            lines.append(text)
            used += len(text) + 1
            shown.extend(syms)
        # Questions about process or rationale are answered by docs and config, not code: quote more of them.
        doc_question = bool(retrieval.intents(task) & {"decision", "workflow", "convention", "dependency"})
        targets = [s.name for s in shown if not is_test_path(s.path)][:3]
        if edit:
            # A change request needs the place to edit, how the surrounding code does it, and little else:
            # the agent must read the file with its own tool before editing anyway.
            notes = self._notes(knowledge, quoted=0, max_why=1, max_pointers=0)
            editable = [s for s in ranked if not is_test_path(s.path) and s.kind in ("function", "method", "class", "variable")]
            if not editable:
                lines += ["", "No matching code found; fall back to searching the repository."]
                return "\n".join(lines), targets
            primary = editable[0]
            # Read range: the best symbol plus other top symbols in the same file, while the span stays small.
            start, end = primary.line, primary.end_line or primary.line
            for other in editable[1:4]:
                if other.path != primary.path:
                    continue
                lo, hi = min(start, other.line), max(end, other.end_line or other.line)
                if hi - lo <= EDIT_READ_MAX_LINES:
                    start, end = lo, hi
            if read_lines:
                style = self._style_examples(task, primary, read_lines)
                if style:
                    lines += ["", "Existing patterns to follow:", *style]
            if notes:
                lines += ["", *notes]
            lines += ["", f"To edit: Read {primary.path} with offset {max(1, start - 2)} and limit {end - start + 5} "
                          f"(your Read tool is required before Edit), then make the change. No need to open() it first."]
            return "\n".join(lines), targets
        notes = self._notes(knowledge, quoted=DOCS_QUOTED_DOC_QUESTION if doc_question else DOCS_QUOTED,
                            quote_chars=DOC_QUOTE_CHARS_DOC_QUESTION if doc_question else DOC_QUOTE_CHARS)
        if notes:
            lines += ["", *notes]
        lines += ["", "Exact code: open(\"<name>\") e.g. " + ", ".join(f'open("{t}")' for t in targets[:2])
                  + ", or read the file at the line ranges above." if targets else "No matching code found; fall back to searching the repository."]
        return "\n".join(lines), targets

    def _style_examples(self, task: str, primary: Sym, read_lines: Callable[[str], list[str]], limit: int = 3) -> list[str]:
        """Real usages of what the task asks to add, in the target's file or, failing that, its package.

        Only task words the target does not already cover count (for "log how many checkouts were
        queued" in a checkout task, that leaves "log"), and a line only counts when it *uses* a matching
        name (a call or attribute access such as ``task_logger.info(``), not an import or prose.
        """
        covered = set(terms(" ".join([primary.name, *primary.refs, primary.path])))
        # Words of the instruction itself ("log how many checkouts it queued"), not of the background
        # ("…when old keys are failing" describes a symptom, not something to add).
        clauses = [c.strip() for c in re.split(r"[.;:,!?\n]+|\band\b", task) if _CHANGE_VERBS.match(c.strip())]
        wanted: set[str] = set()
        for q in terms(" ".join(clauses) or task):
            if not any(term_match(q, c) for c in covered):
                wanted.add(q)
                wanted.update(expand_terms([q])[q])
        if not wanted:
            return []

        def scan(path: str, lo: int = 1, hi: int | None = None, skip: tuple[int, int] = (0, -1)) -> list[str]:
            try:
                text_lines = read_lines(path)
            except Exception:  # unreadable file: no examples rather than an error
                return []
            prose = _string_lines("\n".join(text_lines)) if path.endswith(".py") else set()
            found = []
            for n, line in enumerate(text_lines[lo - 1:hi], lo):
                code = line.split("#", 1)[0].strip()
                if (not code or n in prose or skip[0] <= n <= skip[1]
                        or code.startswith(("import ", "from ", '"', "'", "def ", "async def ", "class ", "@"))):
                    continue
                used = {m.group(1) for m in _USAGE.finditer(code)}
                if any(term_match(w, t) for name in used for t in terms(name) for w in wanted):
                    found.append(f"  {path}:{n}: {clean_text(code, 140)}")
            return found

        out = scan(primary.path, skip=(primary.line, primary.end_line or primary.line))[:limit]
        if out:
            return out
        # Nothing in the file itself: look at symbols in the same package that reference a matching name.
        package = primary.path.rsplit("/", 1)[0] + "/" if "/" in primary.path else ""
        q = fts_query(sorted(wanted))
        if not q:
            return []
        rows = self.conn.execute(
            "SELECT path, name, line FROM symbols_fts WHERE symbols_fts MATCH ? AND repo_id=? LIMIT 100",
            (f"refs : ({q})", self.repo_id)).fetchall()
        for r in rows:
            if not r["path"].startswith(package) or is_test_path(r["path"]) or _PERIPHERAL.search(r["path"]):
                continue
            for sym in self._rows("path=? AND name=? AND line=?", (r["path"], r["name"], r["line"])):
                out.extend(scan(sym.path, sym.line, sym.end_line)[:1])
            if len(out) >= limit:
                break
        return out[:limit]

    def _render_symbol(self, s: Sym, qterms: set[str], indent: str) -> list[str]:
        label = s.signature.replace("def ", "").replace("async def ", "async ") if s.kind in ("function", "method") else s.signature or s.name
        rng = f"L{s.line}-{s.end_line}" if s.end_line and s.end_line != s.line else f"L{s.line}"
        first = f"{indent}{label}  {rng}"
        if s.catches:
            first += f" · catches {', '.join(s.catches[:3])}"
        if s.doc:
            first += f" — {clean_text(s.doc, 90)}"
        out = [first]
        if s.refs:
            relevant = [r for r in s.refs if set(terms(r)) & qterms]
            rest = [r for r in s.refs if r not in relevant and r not in s.catches]
            uses = (relevant + rest)[:6]
            detail = f"{indent}    uses {', '.join(uses)}"
        else:
            detail = ""
        all_callers = self.callers(s, limit=20)
        code_callers = [c for c in all_callers if not is_test_path(c.path)][:3]
        n_tests = sum(1 for c in all_callers if is_test_path(c.path))
        callers = [c.short if c.path == s.path else f"{c.short} ({c.path.split('/')[-1]})" for c in code_callers]
        if n_tests:
            callers.append(f"{n_tests} test{'s' if n_tests != 1 else ''}")
        if callers:
            detail = (detail or f"{indent}   ") + f" · called by {', '.join(callers)}"
        if detail:
            out.append(detail)
        return out

    def _notes(self, knowledge: list[retrieval.Ranked], quoted: int = DOCS_QUOTED, quote_chars: int = DOC_QUOTE_CHARS,
               max_why: int = 4, max_pointers: int = 2) -> list[str]:
        """Only what code cannot say: why (commits, decisions), agent findings; docs as pointers."""
        why, docs, used = [], [], 0
        for item in knowledge[:8]:
            r = item.row
            srcs = [x for x in item.sources if x["path"] != "."]
            if r["origin"] == "git":
                files = ", ".join(sorted({x["path"].split("/")[-1] for x in srcs})[:3])
                text = f"- commit {r['title'][11:]}" + (f": {clean_text(r['rationale'], 260)}" if r["rationale"] else "") + (f" ({files})" if files else "")
                why.append(text)
            elif r["origin"] == "agent" or r["category"] == "decision":
                tag = "agent finding" + ("" if r["verification"] == "verified" else ", unverified") if r["origin"] == "agent" else "decision"
                if r["status"] == "stale":
                    tag += ", STALE: a cited file changed"
                why.append(f"- [{tag}] {clean_text(r['content'], 260)}" + (f" Why: {clean_text(r['rationale'], 160)}" if r["rationale"] else ""))
            elif srcs and srcs[0].get("start_line") and not srcs[0]["path"].endswith((".py", ".js", ".ts", ".go", ".rs", ".java")):
                # Docs and config: the best two are quoted (workflows, commands and rationale live there,
                # not in code); the rest are pointers the agent can open.
                s0 = srcs[0]
                where = f"{s0['path']} L{s0['start_line']}-{s0.get('end_line') or s0['start_line']}"
                if len(docs) < quoted:
                    docs.append(f"- {where} · {clean_text(r['title'], 70)}: {clean_text(r['content'], quote_chars)}")
                elif len(docs) < quoted + max_pointers:
                    docs.append(f"- {where}: {clean_text(r['title'], 80)}")
        out: list[str] = []
        if why:
            out.append("Why / history:")
            for w in why[:max_why]:
                if used + len(w) > NOTES_MAX_CHARS:
                    break
                out.append(w)
                used += len(w)
        if docs:
            out.append("Docs & config:")
            out.extend(docs[:quoted + max_pointers])
        return out

    # --------------------------------------------------------------- open
    def resolve(self, target: str) -> list[Sym]:
        cands = self.by_name(target.strip())
        non_test = [c for c in cands if not is_test_path(c.path)]
        return non_test or cands

    def neighbours_text(self, s: Sym) -> list[str]:
        out = []
        callees = self.callees(s, limit=6)
        if callees:
            out.append("Calls:")
            out += [f"  {c.name}  {c.path}:{c.line}  {c.signature}" for c in callees]
        callers = self.callers(s, limit=6)
        if callers:
            out.append("Called by:")
            out += [f"  {c.name}  {c.path}:{c.line}" + ("  (test)" if is_test_path(c.path) else "") for c in callers]
        return out

    def file_skeleton(self, path: str, max_chars: int = MAP_MAX_CHARS) -> str:
        syms = self._rows("path=? ORDER BY line", (path,))
        lines = [f"{path} (skeleton: signatures only)"]
        for s in syms:
            if s.kind in ("attribute", "route"):
                continue
            rng = f"L{s.line}-{s.end_line}" if s.end_line and s.end_line != s.line else f"L{s.line}"
            indent = "    " if s.owner else "  "
            label = s.signature or s.name
            lines.append(f"{indent}{label}  {rng}" + (f" — {clean_text(s.doc, 80)}" if s.doc else ""))
        text = "\n".join(lines)
        return text if len(text) <= max_chars else clean_text(text, max_chars)
