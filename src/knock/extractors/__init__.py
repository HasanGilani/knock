"""Deterministic knowledge extraction.

Each extractor turns one file's (redacted) text into:

* ``Candidate`` knowledge items — compact, source-grounded facts with a stable key
  inside the file, so re-extraction can match, update or retire them; and
* ``Symbol`` records — names and line numbers used to locate implementations.

Extractors are pure functions of ``(path, text)``. Bump ``EXTRACTOR_VERSION`` whenever
their output changes so that unchanged files are re-extracted once.

Additional strategies (for example an LLM-backed extractor) can be added by
registering another function in ``extract_file``; they must keep the same contract
and must not run unless explicitly configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EXTRACTOR_VERSION = 4

CATEGORIES = (
    "architecture",
    "decision",
    "dependency",
    "workflow",
    "convention",
    "implementation",
    "debugging",
)


@dataclass
class Candidate:
    key: str
    category: str
    title: str
    content: str
    rationale: str | None = None
    entities: list[str] = field(default_factory=list)
    start_line: int | None = None
    end_line: int | None = None
    confidence: float = 0.9
    status: str = "active"            # extracted items may be born 'superseded' (e.g. an ADR marked superseded)
    status_reason: str | None = None


@dataclass
class Symbol:
    name: str
    kind: str
    line: int
    signature: str = ""
    refs: list[str] = field(default_factory=list)   # what the body calls, catches, raises and reads


@dataclass
class FileExtraction:
    candidates: list[Candidate] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    doc: str = ""                     # leading module docstring/comment, if any


def extract_file(path: str, text: str, language: str, kind: str) -> FileExtraction:
    from . import code, docs, manifests

    result = FileExtraction()
    if kind == "code":
        result = code.extract(path, text, language)
    elif kind == "doc":
        result = docs.extract(path, text, language)
    # Manifests and config files can also be code (e.g. setup.py is not handled; noxfile.py is).
    result.candidates.extend(manifests.extract(path, text, language))
    for c in result.candidates:
        if c.category not in CATEGORIES:
            raise ValueError(f"extractor produced unknown category {c.category!r}")
    return result
