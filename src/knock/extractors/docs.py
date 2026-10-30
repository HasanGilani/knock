"""Documentation extraction (Markdown and reStructuredText).

Each section with substantive text becomes one knowledge item: a condensed
statement of the section plus its line range, so the full text can always be
fetched from the original file. Architecture Decision Records become ``decision``
items with their context and consequences kept as rationale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..util import clean_text, first_sentences
from . import Candidate, FileExtraction

SECTION_CHARS = 700
MIN_SECTION_CHARS = 40
MAX_SECTIONS = 80

_CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("decision", re.compile(r"\b(decisions?|rationale|why\b|design (choices?|decisions?)|trade-?offs?|alternatives|motivation|adr\b)", re.I)),
    ("debugging", re.compile(r"\b(troubleshoot\w*|known (issues?|bugs?|problems?)|faq|gotchas?|caveats?|pitfalls?|common (errors?|problems?|issues?)|debug\w*|workarounds?|limitations?|errors?)\b", re.I)),
    ("workflow", re.compile(r"\b(install\w*|setup|set up|getting started|quick ?start|build\w*|test\w*|running|run\b|develop\w*|deploy\w*|release\w*|ci\b|continuous|publish\w*|usage|commands?|scripts?|docker|production|hosting|server|environment|migrat\w*|upgrad\w*)\b", re.I)),
    ("convention", re.compile(r"\b(style|conventions?|guidelines?|contribut\w*|code review|naming|lint\w*|format\w*|commit messages?|pull requests?|coding standards?|best practices?|pre-commit)\b", re.I)),
    ("dependency", re.compile(r"\b(dependenc\w*|requirements?|integrations?|third[- ]party|prerequisites?|external services?|compatib\w*)\b", re.I)),
    ("architecture", re.compile(r"\b(architecture|overview|design|structure|components?|modules?|layout|data ?flow|how it works|internals|system|concepts?|lifecycle|context|introduction|about)\b", re.I)),
]
_DEPLOY = re.compile(r"\b(deploy\w*|production|hosting|release\w*|publish\w*|docker|kubernetes|k8s|server|wsgi|asgi|nginx|apache|gunicorn|uwsgi)\b", re.I)
_ADR_PATH = re.compile(r"(^|/)(adr|adrs|decisions?|architecture[-_]decisions?)(/|$)|(^|/)adr[-_]?\d+|(^|/)\d{3,4}-[\w-]+\.md$", re.I)
_SKIP_DOCS = re.compile(r"(^|/)(changelog|changes|history|news|release[-_]?notes|authors|contributors|license|licence|copying|notice|code_of_conduct)(\.\w+)?$", re.I)
_SHELL_FENCE = re.compile(r"^(bash|sh|shell|console|zsh|powershell|ps1|cmd|text)?$", re.I)


@dataclass
class Section:
    level: int
    title: str
    path: list[str]
    start: int                      # 1-based line of heading
    end: int = 0
    body: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


def extract(path: str, text: str, language: str) -> FileExtraction:
    out = FileExtraction()
    if language not in ("markdown", "rst") or _SKIP_DOCS.search(path):
        return out
    sections = _parse_markdown(text) if language == "markdown" else _parse_rst(text)
    doc_title = next((s.title for s in sections if s.level == 1), PurePosixPath(path).stem)
    if _ADR_PATH.search(path) or _looks_like_adr(sections):
        adr = _adr(path, doc_title, sections)
        if adr:
            out.candidates.append(adr)
            return out
    seen_titles: dict[str, int] = {}
    for sec in sections[:MAX_SECTIONS]:
        body = _condense(sec.body)
        if len(body) < MIN_SECTION_CHARS and not sec.commands:
            continue
        category = _categorize(path, sec)
        commands = sec.commands[:6]
        content = first_sentences(body, SECTION_CHARS) if body else ""
        if commands and category in ("workflow", "debugging", "convention"):
            # Commands lead: they are the most reusable part and survive truncation in compact views.
            content = ("Commands: " + "; ".join(f"`{c}`" for c in commands) + ". " + content).strip()
        if not content:
            continue
        path_parts = sec.path[1:] if sec.path[:1] == [doc_title] and len(sec.path) > 1 else sec.path
        heading = " › ".join(path_parts) if path_parts else doc_title
        n = seen_titles.get(heading, 0)
        seen_titles[heading] = n + 1
        key = f"section:{heading}" + (f"#{n}" if n else "")
        entities = [w.lower() for w in re.findall(r"`([^`]{2,40})`", " ".join(sec.body))][:12]
        if category == "workflow" and _DEPLOY.search(heading + " " + path):
            entities.append("deployment")
        out.candidates.append(
            Candidate(
                key=key,
                category=category,
                title=f"{doc_title} › {heading}" if heading != doc_title else doc_title,
                content=clean_text(content, SECTION_CHARS + 300),
                entities=sorted(set(entities)),
                start_line=sec.start,
                end_line=sec.end,
                confidence=0.9 if _is_primary_doc(path) else 0.8,
            )
        )
    return out


def _is_primary_doc(path: str) -> bool:
    p = path.lower()
    return "/" not in p or p.startswith(("docs/", "doc/", "documentation/"))


def _categorize(path: str, sec: Section) -> str:
    heading = " ".join(sec.path[-2:]) if sec.path else ""
    for category, rule in _CATEGORY_RULES:
        if rule.search(heading):
            return category
    name = PurePosixPath(path).stem
    for category, rule in _CATEGORY_RULES:
        if rule.search(name.replace("-", " ").replace("_", " ")):
            return category
    if path.lower().startswith("readme") or sec.level <= 1:
        return "architecture"
    return "implementation"


# --------------------------------------------------------------------- markdown
_ATX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+-]*)")


def _parse_markdown(text: str) -> list[Section]:
    lines = text.splitlines()
    sections: list[Section] = [Section(0, "", [], 1)]
    stack: list[tuple[int, str]] = []
    fence: str | None = None
    fence_shell = False
    in_front_matter = bool(lines and lines[0].strip() == "---")
    for i, line in enumerate(lines, 1):
        if in_front_matter:
            if i > 1 and line.strip() == "---":
                in_front_matter = False
            continue
        m = _FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            elif fence_shell:
                cmd = line.strip()
                cmd = cmd[2:] if cmd.startswith("$ ") else cmd
                if cmd and not cmd.startswith("#") and len(cmd) < 160:
                    sections[-1].commands.append(cmd)
            continue
        if m:
            fence = m.group(1)
            fence_shell = bool(_SHELL_FENCE.match(m.group(2) or ""))
            continue
        h = _ATX.match(line)
        setext = None
        if not h and i < len(lines) and line.strip() and not line.startswith((" ", "\t", "-", "*", ">", "|")):
            nxt = lines[i]  # next line (0-based index i)
            if re.match(r"^=+\s*$", nxt):
                setext = 1
            elif re.match(r"^-{3,}\s*$", nxt):
                setext = 2
        if h or setext:
            level = len(h.group(1)) if h else setext
            title = _strip_inline(h.group(2) if h else line.strip())
            sections[-1].end = i - 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sections.append(Section(level, title, [t for _, t in stack], i))
            continue
        if setext is None and re.match(r"^(=+|-{3,})\s*$", line) and sections[-1].start == i - 1:
            continue  # setext underline
        sections[-1].body.append(line)
    sections[-1].end = len(lines)
    return [s for s in sections if s.level > 0 or _condense(s.body)]


# -------------------------------------------------------------------------- rst
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+:.])\1{2,}\s*$")


def _parse_rst(text: str) -> list[Section]:
    lines = text.splitlines()
    sections: list[Section] = [Section(0, "", [], 1)]
    levels: list[str] = []      # adornment styles in order of first appearance
    stack: list[tuple[int, str]] = []
    in_code = False
    code_indent = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        prev_overline = i > 0 and _RST_UNDERLINE.match(lines[i - 1] or "") is not None
        if (
            line.strip()
            and not line.startswith(" ")
            and _RST_UNDERLINE.match(nxt)
            and len(nxt.strip()) >= len(line.strip()) - 1
            and not _RST_UNDERLINE.match(line)
        ):
            style = nxt.strip()[0] + ("o" if prev_overline else "")
            if style not in levels:
                levels.append(style)
            level = levels.index(style) + 1
            title = _strip_inline(line.strip())
            if prev_overline and sections[-1].body and _RST_UNDERLINE.match(sections[-1].body[-1] or ""):
                sections[-1].body.pop()
            sections[-1].end = i
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sections.append(Section(level, title, [t for _, t in stack], i + 1))
            in_code = False
            i += 2
            continue
        stripped = line.strip()
        if in_code:
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= code_indent:
                in_code = False
            else:
                if stripped and code_shell:
                    cmd = stripped[2:] if stripped.startswith("$ ") else stripped
                    if not cmd.startswith("#") and len(cmd) < 160:
                        sections[-1].commands.append(cmd)
                i += 1
                continue
        m = re.match(r"^(\s*)\.\.\s+(code-block|code|sourcecode|prompt)::\s*(\S*)", line)
        if m or stripped.endswith("::") and not stripped.startswith(".."):
            in_code = True
            code_indent = len(line) - len(line.lstrip())
            code_shell = bool(m and (m.group(2) == "prompt" or _SHELL_FENCE.match(m.group(3) or "x") and m.group(3)))
            if not m:
                sections[-1].body.append(line.rstrip(":") + ":")
            i += 1
            continue
        if stripped.startswith(".. ") and not stripped.startswith((".. note::", ".. warning::", ".. versionadded", ".. versionchanged", ".. deprecated", ".. admonition")):
            i += 1
            continue
        sections[-1].body.append(line)
        i += 1
    sections[-1].end = len(lines)
    return [s for s in sections if s.level > 0 or _condense(s.body)]


# ---------------------------------------------------------------------- helpers
def _strip_inline(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r":[\w:]+:`~?([^`<]+?)(?:\s*<[^>]+>)?`", r"\1", text)  # rst roles
    text = re.sub(r"`([^`<]+?)\s*<[^>]+>`_", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("**", "").replace("``", "`")
    return text.strip()


def _condense(lines: list[str]) -> str:
    kept: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("|", "<!--", "[!", "[![")) or re.match(r"^[-=*_]{3,}$", s):
            continue
        if s.startswith(".. ") and "::" in s:
            s = re.sub(r"^\.\.\s+[\w-]+::\s*", "", s)
        s = re.sub(r"^[-*+]\s+|^\d+[.)]\s+", "", s)
        kept.append(_strip_inline(s))
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _looks_like_adr(sections: list[Section]) -> bool:
    names = {s.title.lower() for s in sections}
    return "decision" in names and ("context" in names or "status" in names)


def _adr(path: str, doc_title: str, sections: list[Section]) -> Candidate | None:
    by_name: dict[str, Section] = {}
    for s in sections:
        key = s.title.lower().split(":")[0].strip()
        by_name.setdefault(key, s)

    def body(*names: str) -> str:
        for n in names:
            for k, s in by_name.items():
                if k.startswith(n):
                    return _condense(s.body)
        return ""

    decision = body("decision", "decision outcome", "chosen option")
    if not decision:
        return None
    context = body("context", "problem", "background", "context and problem")
    consequences = body("consequences", "implications", "results", "pros and cons")
    status_text = body("status").lower()
    if not status_text:
        status_text = " ".join(_condense(s.body) for s in sections[:2]).lower()
    status, reason = "active", None
    sm = re.search(r"\b(superseded|deprecated|rejected|proposed|accepted)\b", status_text)
    adr_status = sm.group(1) if sm else None
    if adr_status in ("superseded", "deprecated"):
        status, reason = "superseded", f"ADR status: {adr_status}"
    rationale = " ".join(
        x for x in (f"Context: {first_sentences(context, 450)}" if context else "",
                    f"Consequences: {first_sentences(consequences, 350)}" if consequences else "") if x
    ) or None
    prefix = f"[{adr_status}] " if adr_status in ("proposed", "rejected") else ""
    return Candidate(
        key="adr",
        category="decision",
        title=doc_title,
        content=prefix + first_sentences(decision, 600),
        rationale=rationale,
        entities=["adr"] + ([adr_status] if adr_status else []),
        start_line=1,
        end_line=max((s.end for s in sections), default=1),
        confidence=0.95,
        status=status,
        status_reason=reason,
    )
