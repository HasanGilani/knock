"""Small, dependency-free helpers shared across modules."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from datetime import datetime, timezone

_IDENT_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does doing for from had has have how i if in
    into is it its it's me my no not of on or our should so than that the their them then there these
    they this those to too use used uses using was we were what when where which while who why will with
    would you your about after again all also any because before between both each few further here
    just more most only other out over own same some such through under until up very via vs get gets
    find show tell explain describe look need want make makes made let lets please file files code
    repo repository project investigate investigating report reports reported implement implemented
    implementing add adding change changing modify modifying new existing following based currently
    someone users""".split()
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def split_identifier(word: str) -> list[str]:
    """Split ``parseHTTPRequest_v2`` into ``['parse', 'http', 'request', 'v', '2']``."""
    parts: list[str] = []
    for chunk in word.split("_"):
        parts.extend(p.lower() for p in _IDENT_PARTS.findall(chunk))
    return parts


def terms(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Tokenise free text or code into lowercase search terms (deduplicated, ordered)."""
    seen: dict[str, None] = {}
    for word in _WORDS.findall(text):
        candidates = [word.lower()]
        sub = split_identifier(word)
        if len(sub) > 1:
            candidates.extend(sub)
        for c in candidates:
            if len(c) < 2 and not c.isdigit():
                continue
            if not keep_stopwords and c in STOPWORDS:
                continue
            seen.setdefault(c, None)
    return list(seen)


def normalize_text(text: str) -> str:
    """Normalisation used for exact-duplicate fingerprints."""
    text = text.lower()
    text = re.sub(r"[`*_>#\"'“”‘’()\[\]{}]", " ", text)
    text = re.sub(r"[.,;:!?]+(\s|$)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def estimate_tokens(chars: int) -> int:
    """Rough token estimate (~4 characters per token). Always reported as an estimate."""
    return math.ceil(chars / 4)


def clean_text(text: str, limit: int | None = None) -> str:
    """Strip control characters and optionally truncate at a word boundary."""
    text = _CONTROL.sub("", text).strip()
    if limit is not None and len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(" ")
        if space > limit * 0.7:
            cut = cut[:space]
        text = cut.rstrip(" ,;:") + " …"
    return text


def first_sentences(text: str, limit: int) -> str:
    """Truncate to whole sentences where possible."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit * 0.5:
        return cut[: end + 1]
    return clean_text(text, limit)


def term_match(query_term: str, text_term: str) -> bool:
    """Loose match tolerant of plurals/inflections: equal, or one is a >=4-char prefix of the other."""
    if query_term == text_term:
        return True
    short, long_ = sorted((query_term, text_term), key=len)
    return len(short) >= 4 and long_.startswith(short[: max(4, len(short) - 2)]) and len(long_) - len(short) <= 5
