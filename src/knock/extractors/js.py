"""JavaScript / TypeScript extraction: symbols with line ranges, and what each body calls and uses.

A small tokenizer (strings, template literals, comments, regex literals and JSX are understood, so
braces inside them do not confuse the structure) feeds a structural pass that finds functions, arrow
functions, classes and their members, interfaces, types, enums, routes and test blocks. For each body
it records the same kind of ``refs`` the Python extractor does: calls, ``new X``, member calls,
constants and classes, JSX components and ``process.env`` keys, plus errors checked in ``catch``
blocks. Anything the tokenizer cannot handle raises, and the caller falls back to line patterns.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from ..util import first_sentences
from . import FileExtraction, Symbol

MAX_REFS = 40
MAX_FILE_CHARS = 1_500_000
MAX_AVG_LINE_CHARS = 300       # longer on average means minified/generated code: leave it to the line patterns

_IDENT = re.compile(r"[A-Za-z_$\u0080-￿][\w$\u0080-￿]*")
_NUMBER = re.compile(r"0[xXbBoO][\da-fA-F_]+n?|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?n?")
_SPACE = re.compile(r"[ \t\r\f\v ﻿  ]+")
_STRING = {'"': re.compile(r'"(?:[^"\\\n]|\\.|\\\n)*"?'), "'": re.compile(r"'(?:[^'\\\n]|\\.|\\\n)*'?")}
_REGEX = re.compile(r"/(?![*/])(?:[^/\\\[\n]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[A-Za-z]*")
_PUNCT = re.compile(r"\?\?=|\.\.\.|===|!==|\*\*=|&&=|\|\|=|=>|==|!=|<=|>=|&&|\|\||\?\?|\?\.(?!\d)|\+\+|--|"
                    r"\+=|-=|\*=|/=|%=|&=|\|=|\^=|\*\*|[(){}\[\];,<>+\-*/%&|^!~?:=.@#]")
_JSX_NAME = re.compile(r"[A-Za-z_$][\w$.:-]*")
_JSX_ATTR = re.compile(r"[A-Za-z_$][\w$:.-]*")

# After these, a "/" starts a regex and a "<" may start JSX (an expression is expected).
_EXPR_KEYWORDS = frozenset("return typeof instanceof in of new delete void throw case do else yield await".split())
_KEYWORDS = frozenset("""if else for while do switch case break continue return function class const let var new delete
    typeof instanceof in of void throw try catch finally import export default extends implements super this yield await
    async static get set as from type interface enum declare namespace module abstract readonly public private protected
    keyof infer is asserts satisfies unique null undefined true false override accessor debugger with""".split())
_NOISE = frozenset("""console log info warn error debug trace push pop shift unshift map filter forEach reduce find
    findIndex some every includes indexOf slice splice concat join split trim replace replaceAll match test then catch
    finally length toString valueOf JSON parse stringify Object keys values entries assign freeze Array isArray from of
    Promise resolve reject all allSettled race Math floor ceil round max min abs random Date now Number parseInt
    parseFloat String Boolean Symbol Map Set WeakMap WeakSet Error require setTimeout clearTimeout setInterval
    clearInterval get set has add call apply bind useState useEffect useMemo useCallback useRef useContext useReducer
    useLayoutEffect expect describe it test beforeEach afterEach beforeAll afterAll vi jest fn mock spyOn
    Fragment React Response Request Headers URL URLSearchParams FormData Blob File ReadableStream WritableStream
    TextEncoder TextDecoder Uint8Array ArrayBuffer DataView AbortController AbortSignal Event CustomEvent EventTarget
    HTMLElement Element Node Window Document KeyboardEvent MouseEvent PointerEvent Record Partial Pick Omit Readonly
    Required Exclude Extract ReturnType Parameters Awaited NonNullable InstanceType Function Infinity NaN""".split())
_MATCHER = re.compile(r"^(?:to(?:Be|Have|Equal|Match|Contain|Throw|StrictEqual)|not$|resolves$|rejects$)")
_ROUTE_OBJECTS = frozenset("app router server api route routes routers fastify".split())
_ROUTE_METHODS = frozenset("get post put patch delete all head options use".split())
_TEST_CALLS = frozenset("describe it test suite context".split())
_CONFIG_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{2,60}$")
_MODIFIERS = frozenset("public private protected static readonly abstract override declare accessor async".split())
_DIRECTIVE = re.compile(r"^(?:eslint|@ts-|prettier|istanbul|tslint|global |jshint|c8 |#region|#endregion)", re.I)

# Tokens are tuples: (kind, value, line, offset). Kinds: "id", "p" (punctuation), "lit" (string,
# number, regex, template), "jsx" (a JSX tag name).
ID, P, LIT, JSX = "id", "p", "lit", "jsx"


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- tokenizer
class _Lexer:
    def __init__(self, text: str, jsx: bool):
        self.text = text
        self.n = len(text)
        self.pos = 0
        self.line = 1
        self.jsx = jsx
        self.tokens: list[tuple[str, str, int, int]] = []
        self.comments: list[tuple[int, int, str, int]] = []   # (start line, end line, text, offset)

    def _advance(self, end: int) -> None:
        self.line += self.text.count("\n", self.pos, end)
        self.pos = end

    def _expr_expected(self) -> bool:
        if not self.tokens:
            return True
        kind, value, _, _ = self.tokens[-1]
        if kind == ID:
            return value in _EXPR_KEYWORDS
        if kind == P:
            return value not in (")", "]", "}")
        return False

    def run(self) -> None:
        if self.text.startswith("#!"):
            nl = self.text.find("\n")
            self._advance(self.n if nl < 0 else nl)
        self.code(stop_at_brace=False)

    def code(self, stop_at_brace: bool) -> None:
        """Lex code until the end, or (inside `${…}` / JSX `{…}`) until the matching close brace."""
        text, depth = self.text, 0
        while self.pos < self.n:
            c = text[self.pos]
            if c == "\n":
                self.line += 1
                self.pos += 1
                continue
            m = _SPACE.match(text, self.pos)
            if m:
                self.pos = m.end()
                continue
            start, line = self.pos, self.line
            if c == "/" and self.pos + 1 < self.n and text[self.pos + 1] in "/*":
                end = text.find("\n", self.pos) if text[self.pos + 1] == "/" else text.find("*/", self.pos + 2)
                end = self.n if end < 0 else (end if text[self.pos + 1] == "/" else end + 2)
                body = text[self.pos:end]
                self._advance(end)
                self.comments.append((line, self.line, body, start))
                continue
            if c in "\"'":
                m = _STRING[c].match(text, self.pos)
                self._advance(m.end())
                self.tokens.append((LIT, m.group(), line, start))
                continue
            if c == "`":
                self.pos += 1
                self.template()
                self.tokens.append((LIT, "`", line, start))
                continue
            if c == "/" and self._expr_expected():
                m = _REGEX.match(text, self.pos)
                if m:
                    self._advance(m.end())
                    self.tokens.append((LIT, "/regex/", line, start))
                    continue
            if c == "<" and self.jsx and self._expr_expected() and self._jsx_start():
                self.jsx_element()
                continue
            m = _IDENT.match(text, self.pos)
            if m:
                self.pos = m.end()
                self.tokens.append((ID, m.group(), line, start))
                continue
            if c.isdigit() or (c == "." and self.pos + 1 < self.n and text[self.pos + 1].isdigit()):
                m = _NUMBER.match(text, self.pos)
                if m and m.end() > self.pos:
                    self.pos = m.end()
                    self.tokens.append((LIT, m.group(), line, start))
                    continue
            m = _PUNCT.match(text, self.pos)
            if not m:
                self.pos += 1          # stray character (e.g. a backslash): skip it
                continue
            value = m.group()
            if stop_at_brace:
                if value == "{":
                    depth += 1
                elif value == "}":
                    if depth == 0:
                        self.pos = m.end()
                        return
                    depth -= 1
            self.pos = m.end()
            self.tokens.append((P, value, line, start))
        if stop_at_brace:
            raise ScanError("unterminated template or JSX expression")

    def template(self) -> None:
        """After an opening backtick: skip text, lex `${…}` expressions, stop after the closing backtick."""
        text = self.text
        while self.pos < self.n:
            c = text[self.pos]
            if c == "\\":
                self._advance(self.pos + 2)
            elif c == "`":
                self.pos += 1
                return
            elif c == "$" and text.startswith("${", self.pos):
                self.pos += 2
                self.code(stop_at_brace=True)
            else:
                if c == "\n":
                    self.line += 1
                self.pos += 1
        raise ScanError("unterminated template literal")

    def _jsx_start(self) -> bool:
        """`<Tag`, `<tag`, `<>` start JSX; `<T,>(` and `<T extends …>` are TSX generics."""
        m = _JSX_NAME.match(self.text, self.pos + 1)
        if self.text.startswith("<>", self.pos):
            return True
        if not m:
            return False
        after = self.text[m.end():m.end() + 9].lstrip()
        return not (after.startswith(",") or after.startswith("extends "))

    def jsx_element(self) -> None:
        text = self.text
        line, start = self.line, self.pos
        self.pos += 1
        if text.startswith(">", self.pos):          # fragment <>…</>
            self.pos += 1
            self._jsx_children()
            return
        m = _JSX_NAME.match(text, self.pos)
        if not m:
            raise ScanError("bad JSX tag")
        self.tokens.append((JSX, m.group(), line, start))
        self.pos = m.end()
        # Attributes: name, name="…", name={…}, {...spread}, then "/>" or ">".
        while self.pos < self.n:
            m = _SPACE.match(text, self.pos)
            if m:
                self.pos = m.end()
                continue
            c = text[self.pos]
            if c == "\n":
                self.line += 1
                self.pos += 1
            elif text.startswith("/>", self.pos):
                self.pos += 2
                return
            elif c == ">":
                self.pos += 1
                self._jsx_children()
                return
            elif c == "{":
                self.pos += 1
                self.code(stop_at_brace=True)
            elif c == "=":
                self.pos += 1
            elif c in "\"'":
                end = text.find(c, self.pos + 1)
                if end < 0:
                    raise ScanError("unterminated JSX attribute")
                self._advance(end + 1)
            elif c == "<":
                self.jsx_element()
            else:
                m = _JSX_ATTR.match(text, self.pos)
                if not m:
                    raise ScanError("bad JSX attribute")
                self.pos = m.end()
        raise ScanError("unterminated JSX tag")

    def _jsx_children(self) -> None:
        text = self.text
        while self.pos < self.n:
            nxt = min((i for i in (text.find("<", self.pos), text.find("{", self.pos)) if i >= 0), default=-1)
            if nxt < 0:
                break
            self._advance(nxt)
            if text.startswith("</", self.pos):
                end = text.find(">", self.pos)
                if end < 0:
                    break
                self._advance(end + 1)
                return
            if text[self.pos] == "{":
                self.pos += 1
                self.code(stop_at_brace=True)
            else:
                self.jsx_element()
        raise ScanError("unterminated JSX element")


def tokenize(text: str, jsx: bool) -> tuple[list[tuple[str, str, int, int]], list[tuple[int, int, str, int]]]:
    lexer = _Lexer(text, jsx)
    lexer.run()
    return lexer.tokens, lexer.comments


# --------------------------------------------------------------- structure
class _Parser:
    def __init__(self, path: str, text: str, tokens: list, comments: list):
        self.path = path
        self.text = text
        self.t = tokens
        self.n = len(tokens)
        self.match = self._pairs()
        self.doc_by_end: dict[int, tuple[int, str]] = {}
        for start, end, body, offset in comments:
            line_start = text.rfind("\n", 0, offset) + 1
            if not text[line_start:offset].strip():        # a trailing "x(); // note" is not a doc comment
                self.doc_by_end[end] = (start, body)
        self.symbols: list[Symbol] = []

    def _pairs(self) -> dict[int, int]:
        opener = {")": "(", "]": "[", "}": "{"}
        stack: list[int] = []
        pairs: dict[int, int] = {}
        unmatched = 0
        for i, (kind, value, _, _) in enumerate(self.t):
            if kind != P:
                continue
            if value in "([{":
                stack.append(i)
            elif value in ")]}":
                j = len(stack) - 1
                while j >= 0 and self.t[stack[j]][1] != opener[value]:
                    j -= 1
                if j < 0:
                    unmatched += 1
                    continue
                unmatched += len(stack) - 1 - j
                pairs[stack[j]] = i
                del stack[j:]
        unmatched += len(stack)
        if unmatched > max(3, len(pairs) // 50):
            raise ScanError(f"{unmatched} unbalanced brackets")
        return pairs

    # helpers ------------------------------------------------------------
    def v(self, i: int) -> str:
        return self.t[i][1] if 0 <= i < self.n else ""

    def kind(self, i: int) -> str:
        return self.t[i][0] if 0 <= i < self.n else ""

    def line(self, i: int) -> int:
        return self.t[min(max(i, 0), self.n - 1)][2] if self.n else 1

    def close(self, i: int) -> int:
        """Index of the bracket closing the one at ``i`` (or ``i`` itself if unmatched)."""
        return self.match.get(i, i)

    def is_p(self, i: int, value: str) -> bool:
        return 0 <= i < self.n and self.t[i][0] == P and self.t[i][1] == value

    def src(self, a: int, b: int) -> str:
        """Source text from token ``a`` to the end of token ``b``, whitespace collapsed."""
        if a >= self.n:
            return ""
        b = min(b, self.n - 1)
        end = self.t[b][3] + len(self.t[b][1]) if self.t[b][0] != LIT or self.t[b][1] not in ("`", "/regex/") else self.t[b][3] + 1
        return re.sub(r"\s+", " ", self.text[self.t[a][3]:end]).strip()

    def skip_generic(self, i: int) -> int:
        """If ``i`` is "<" opening type parameters/arguments, the index after the matching ">"; else ``i``."""
        if not self.is_p(i, "<"):
            return i
        depth, k, limit = 0, i, min(self.n, i + 200)
        while k < limit:
            kind, v = self.kind(k), self.v(k)
            if kind == P:
                if v == "<":
                    depth += 1
                elif v == ">":
                    depth -= 1
                    if depth == 0:
                        return k + 1
                elif v in "([{":
                    k = self.close(k) + 1
                    continue
                elif v in (";", ")", "]", "}"):
                    return i
            k += 1
        return i

    def skip_type(self, i: int, stops: tuple[str, ...]) -> int:
        """From the token after a ":" in a type position, return the index of the first stop token at depth 0.

        Object-literal types (``{ a: string }``) are skipped when the "{" follows ":", "|", "&", ",", "<",
        "(", "=>" or "keyof"; any other "{" is treated as the start of a body and returned.
        """
        k = i
        angle = 0
        while k < self.n:
            kind, v = self.kind(k), self.v(k)
            if k > i and angle == 0 and self.line(k) > self.line(k - 1) and self._new_statement(k):
                return k
            if kind == P:
                if v in stops and angle == 0 and not (v == "{" and self._type_brace(k)):
                    return k
                if v == "<":
                    angle += 1
                elif v == ">" and angle:
                    angle -= 1
                elif v in "([{":
                    k = self.close(k) + 1
                    continue
                elif v in (";", "}", ")") and angle == 0:
                    return k
            k += 1
        return k

    def _type_brace(self, k: int) -> bool:
        prev = self.v(k - 1)
        return prev in (":", "|", "&", ",", "<", "(", "=>", "keyof", "typeof", "?")

    def doc_for(self, i: int) -> str:
        """First sentence of the JSDoc or line comments directly above token ``i``."""
        target = self.line(i) - 1
        parts: list[str] = []
        while target in self.doc_by_end:
            start, body = self.doc_by_end[target]
            if body.startswith("//"):
                parts.insert(0, body[2:].strip())
                target = start - 1
                continue
            parts.insert(0, body)
            break
        text = " ".join(parts).strip()
        if not text:
            return ""
        text = re.sub(r"^/\*+|\*/$", "", text)
        text = " ".join(line.strip().lstrip("*").strip() for line in text.splitlines())
        text = text.split(" @", 1)[0].strip() if not text.startswith("@") else ""
        if not text or _DIRECTIVE.match(text):
            return ""
        return first_sentences(text, 140)

    # refs ----------------------------------------------------------------
    def refs(self, a: int, b: int) -> tuple[list[str], list[str]]:
        """What tokens ``a``..``b`` call and use, and which error classes their catch blocks test."""
        seen: dict[str, None] = {}
        catches: dict[str, None] = {}

        def add(name: str) -> None:
            if (name and len(seen) < MAX_REFS and name not in _NOISE and name not in _KEYWORDS
                    and not _MATCHER.match(name) and len(name) > 1):
                seen.setdefault(name, None)

        catch_until = -1
        for i in range(a, min(b, self.n)):
            kind, v, _, _ = self.t[i]
            if kind == JSX:
                last = v.split(".")[-1]
                if last[:1].isupper():
                    add(last)
                continue
            if kind == LIT:
                if self.is_p(i - 1, "[") and self.is_p(i + 1, "]") and len(v) > 2 and v[0] in "\"'":
                    key = v[1:-1]
                    if _CONFIG_KEY.match(key):
                        add(key)
                continue
            if kind != ID:
                continue
            if v == "catch" and self.is_p(i + 1, "(") and self.is_p(self.close(i + 1) + 1, "{"):
                catch_until = self.close(self.close(i + 1) + 1)
                continue
            if v == "instanceof" and i < catch_until and self.kind(i + 1) == ID:
                catches.setdefault(self.v(i + 1), None)
                continue
            prev = self.v(i - 1)
            if prev in (".", "?."):
                owner = self.v(i - 2)
                if owner == "env" and self.v(i - 4) in ("process", "meta"):
                    add(v)                                   # process.env.API_KEY, import.meta.env.VITE_URL
                elif self.is_p(i + 1, "(") or self.is_p(i + 1, "?.") or (self.is_p(i + 1, "<") and self.is_p(self.skip_generic(i + 1), "(")):
                    add(v)                                   # member call: api.fetchUser(…)
                elif v[:1].isupper():
                    add(v)                                   # member constant: UserService.MAX_RETRIES, Status.Active
                continue
            if v in _KEYWORDS:
                continue
            nxt = i + 1
            if self.is_p(nxt, "<"):
                nxt = self.skip_generic(nxt)
            if self.is_p(nxt, "(") or prev == "new" or self.kind(nxt) == LIT and self.v(nxt) == "`":
                add(v)                                       # call, constructor, tagged template
            elif v[:1].isupper() and not self.is_p(i + 1, ":"):
                add(v)                                       # constants and classes: MAX_RETRIES, UserService
        return list(seen), list(catches)[:8]

    # statements ----------------------------------------------------------
    def stmt_end(self, i: int, limit: int) -> int:
        """Index just past the statement starting at ``i`` (";"-terminated or ended by a line break)."""
        k = i
        prev_line = self.line(i)
        while k < limit:
            kind, v, line, _ = self.t[k]
            if kind == P:
                if v == ";":
                    return k + 1
                if v in "([{":
                    k = self.close(k) + 1
                    if k <= limit:
                        prev_line = self.line(k - 1)
                    continue
                if v in ")]}":
                    return k
            if line > prev_line and k > i and self._new_statement(k):
                return k
            prev_line = line
            k += 1
        return limit

    # A line ending in ">" is almost always a closed generic (Promise<T>), not a comparison, so "<"/">" are not here.
    _CONTINUES = frozenset(". ?. , = => + - * / % && || ?? ? : | & ( [ { == === != !== <= >= += -= *= /=".split())
    _STARTS = frozenset(". ?. ) ] } , = => ? : + - * / % && || ?? | & == === != !== instanceof in as satisfies".split())

    def _new_statement(self, k: int) -> bool:
        return self.v(k - 1) not in self._CONTINUES and self.v(k) not in self._STARTS

    def tail_end(self, end: int, limit: int) -> int:
        """Extend an expression that ends at token ``end`` through a ";" or a chained continuation."""
        k = end + 1
        if k >= limit:
            return end
        if self.is_p(k, ";"):
            return k
        if self.line(k) > self.line(end) and self.v(k) not in self._STARTS:
            return end                                   # the next line is a new statement
        return max(end, self.stmt_end(k, limit) - 1)

    def body_end(self, i: int, limit: int) -> int:
        """End of a function body starting at token ``i``: a block, or an arrow's expression."""
        if self.is_p(i, "{"):
            return self.close(i)
        return max(i, self.stmt_end(i, limit) - 1)

    def arrow_at(self, i: int) -> int | None:
        """If an arrow function starts at token ``i``, the index of its "=>"."""
        k = i
        if self.v(k) == "async" and self.kind(k) == ID and (self.is_p(k + 1, "(") or self.kind(k + 1) == ID or self.is_p(k + 1, "<")):
            k += 1
        k = self.skip_generic(k)
        if self.kind(k) == ID and self.is_p(k + 1, "=>"):
            return k + 1
        if self.is_p(k, "("):
            k = self.close(k) + 1
            if self.is_p(k, ":"):
                k = self.skip_type(k + 1, ("=>",))
            if self.is_p(k, "=>"):
                return k
        return None

    def function_parts(self, i: int, limit: int) -> tuple[int, int] | None:
        """For a parameter list at ``i``: (index of the body start, index of its last token), or None."""
        if not self.is_p(i, "("):
            return None
        k = self.close(i) + 1
        if self.is_p(k, ":"):
            k = self.skip_type(k + 1, ("{", ";", "=>"))
        if self.is_p(k, "{"):
            return k, self.close(k)
        return None

    def add(self, name: str, kind: str, start: int, end: int, signature: str, body: tuple[int, int] | None,
            extra_refs: list[str] | None = None) -> None:
        refs, catches = self.refs(*body) if body else ([], [])
        if extra_refs:
            refs = [r for r in extra_refs if r and r not in _NOISE] + [r for r in refs if r not in extra_refs]
        self.symbols.append(Symbol(name, kind, self.line(start), signature[:200], refs=refs[:MAX_REFS],
                                   end_line=self.line(end), doc=self.doc_for(start), catches=catches))

    def default_name(self) -> str:
        p = PurePosixPath(self.path)
        stem = p.name.split(".")[0]
        return p.parent.name if stem in ("index", "page", "route", "layout") and p.parent.name else stem

    # module level ----------------------------------------------------------
    def scan_block(self, a: int, b: int) -> None:
        i = a
        while i < b:
            j = self.statement(i, b)
            i = j if j > i else i + 1

    def statement(self, i: int, b: int) -> int:
        start = i
        while self.is_p(i, "@"):                         # decorators
            i = self._skip_decorator(i)
        exported = False
        while self.kind(i) == ID and self.v(i) in ("export", "default", "declare", "abstract"):
            exported = exported or self.v(i) == "export"
            i += 1
        v, kind = self.v(i), self.kind(i)
        if kind == ID and v == "async" and self.v(i + 1) == "function":
            i += 1
            v = "function"
        if kind == ID and v == "function":
            k = i + 1
            if self.is_p(k, "*"):
                k += 1
            name = self.v(k) if self.kind(k) == ID else self.default_name()
            if self.kind(k) == ID:
                k += 1
            k = self.skip_generic(k)
            parts = self.function_parts(k, b)
            if not parts:                                 # overload or declaration without a body
                return self.stmt_end(k, b)
            body, end = parts
            sig = self.src(start if not self.is_p(start, "@") else i, self.close(k))
            sig = re.sub(r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?", "", sig)
            self.add(name, "function", start, end, sig, (body, end))
            return end + 1
        if kind == ID and v == "class":
            return self.class_decl(start, i, b)
        if kind == ID and v in ("interface", "enum") or (v == "const" and self.v(i + 1) == "enum"):
            k = i + 2 if v == "const" else i + 1
            name = self.v(k)
            brace = next((x for x in range(k, min(b, k + 60)) if self.is_p(x, "{")), None)
            if brace is None or self.kind(k) != ID:
                return self.stmt_end(i, b)
            end = self.close(brace)
            ext = []
            if v == "interface":
                ext = [self.v(x) for x in range(k + 1, brace) if self.kind(x) == ID and self.v(x)[:1].isupper()]
            self.add(name, "type" if v == "interface" else "enum", start, end, self.src(i, brace - 1) if brace > i else name,
                     None, extra_refs=ext)
            return end + 1
        if kind == ID and v == "type" and self.kind(i + 1) == ID and (self.is_p(i + 2, "=") or self.is_p(i + 2, "<")):
            end = self.stmt_end(i, b) - 1
            self.add(self.v(i + 1), "type", start, end, self.src(i, i + 1), None)
            return end + 1
        if kind == ID and v == "global" and self.is_p(i + 1, "{"):          # declare global { … }
            self.scan_block(i + 2, self.close(i + 1))
            return self.close(i + 1) + 1
        if kind == ID and v in ("namespace", "module") and (self.kind(i + 1) in (ID, LIT)):
            brace = i + 1                                  # namespace A.B.C { … } / declare module "x" { … }
            while self.kind(brace) in (ID, LIT) or self.is_p(brace, "."):
                brace += 1
            if self.is_p(brace, "{"):
                self.scan_block(brace + 1, self.close(brace))
                return self.close(brace) + 1
            return self.stmt_end(i, b)                     # declare module "*.scss";
        if kind == ID and v in ("const", "let", "var") and self.kind(i + 1) == ID:
            return self.variable(start, i, b, exported)
        if kind == ID and v in ("import",) and not self.is_p(i + 1, "("):
            return self.stmt_end(i, b)
        # export default <expression>: an anonymous component or handler
        if self.v(start) == "export" and self.v(start + 1) == "default" and i == start + 2:
            arrow = self.arrow_at(i)
            if arrow is not None:
                end = self.body_end(arrow + 1, b)
                self.add(self.default_name(), "function", start, end, self.src(i, arrow), (arrow + 1, end + 1))
                return end + 1
            if self.is_p(i, "{"):                        # export default { … } (Vue options, configs)
                self.object_members(self.default_name(), i, start)
                return self.close(i) + 1
            k = i
            while self.kind(k) == ID and self.is_p(k + 1, "."):
                k += 2
            if self.kind(k) == ID and self.is_p(k + 1, "(") and self.is_p(k + 2, "{"):   # defineComponent({ … })
                self.object_members(self.default_name(), k + 2, start)
                return self.close(k + 1) + 1
            return self.stmt_end(i, b)
        # module.exports = … / exports.name = …
        if kind == ID and v in ("module", "exports"):
            return self.commonjs(start, i, b)
        # Top-level calls: routes (app.get('/x', …)) and test blocks (describe('…', …))
        if kind == ID:
            return self.call_statement(start, i, b)
        return self.stmt_end(i, b)

    def _skip_decorator(self, i: int) -> int:
        k = i + 1
        while self.kind(k) == ID or self.is_p(k, "."):
            k += 1
        if self.is_p(k, "("):
            k = self.close(k) + 1
        return k

    def variable(self, start: int, i: int, b: int, exported: bool) -> int:
        name = self.v(i + 1)
        k = i + 2
        if self.is_p(k, "!"):
            k += 1
        if self.is_p(k, ":"):
            k = self.skip_type(k + 1, ("=", ";"))
        if not self.is_p(k, "="):
            return self.stmt_end(i, b)
        rhs = k + 1
        if self.v(rhs) in ("await",):
            rhs += 1
        arrow = self.arrow_at(rhs)
        if arrow is not None:
            end = self.body_end(arrow + 1, b)
            self.add(name, "function", start, end, self.src(i, arrow), (arrow + 1, end + 1))
            return end + 1
        if self.v(rhs) == "function" or (self.v(rhs) == "async" and self.v(rhs + 1) == "function"):
            k = rhs + (2 if self.v(rhs) == "async" else 1)
            if self.is_p(k, "*"):
                k += 1
            if self.kind(k) == ID:
                k += 1
            parts = self.function_parts(k, b)
            if parts:
                body, end = parts
                self.add(name, "function", start, end, f"{name} = {self.src(rhs, self.close(k))}", (body, end))
                return end + 1
        if self.v(rhs) == "class":
            return self.class_decl(start, rhs, b, name=name)
        if self.is_p(rhs, "{"):
            end = max(self.close(rhs), self.stmt_end(rhs, b) - 1)   # also `{ … } as const`
            if self.object_members(name, rhs, start) or exported:
                self.add(name, "variable", start, end, f"{name} = {{…}}", None)
            return end + 1
        # name = callee(…): a wrapped component (memo/forwardRef(() => …)) or a module-level object
        k = rhs
        while self.kind(k) == ID and (self.is_p(k + 1, ".") or self.is_p(k + 1, "?.")):
            k += 2
        k_call = self.skip_generic(k + 1) if self.kind(k) == ID else k
        if self.kind(k) == ID and self.is_p(k_call, "("):
            end = self.close(k_call)
            callee = self.v(k)
            inner = any(self.v(x) == "=>" or self.v(x) == "function" for x in range(k_call, end))
            end = self.tail_end(end, b)
            kind = "function" if inner else "variable"
            self.add(name, kind, start, end, f"{name} = {self.src(rhs, k)}(…)", (k_call, end + 1), extra_refs=[callee])
            return end + 1
        end = self.stmt_end(i, b) - 1
        if name.isupper() and len(name) > 2:
            self.add(name, "constant", start, end, name, None)
        elif exported:
            self.add(name, "export", start, end, self.src(i, i + 1), (rhs, end + 1))
        return end + 1

    def commonjs(self, start: int, i: int, b: int) -> int:
        k = i
        names = []
        while self.kind(k) == ID and (self.is_p(k + 1, ".") or self.is_p(k + 1, "=")):
            names.append(self.v(k))
            if self.is_p(k + 1, "="):
                break
            k += 2
        if not self.is_p(k + 1, "=") or names[:1] not in (["module"], ["exports"]):
            return self.call_statement(start, i, b)
        target = names[-1] if names[-1] not in ("exports", "module") else self.default_name()
        rhs = k + 2
        arrow = self.arrow_at(rhs)
        if arrow is not None:
            end = self.body_end(arrow + 1, b)
            self.add(target, "function", start, end, f"{target} = {self.src(rhs, arrow)}", (arrow + 1, end + 1))
            return end + 1
        if self.v(rhs) == "function" or (self.v(rhs) == "async" and self.v(rhs + 1) == "function"):
            kk = rhs + (2 if self.v(rhs) == "async" else 1)
            if self.kind(kk) == ID:
                kk += 1
            parts = self.function_parts(kk, b)
            if parts:
                body, end = parts
                self.add(target, "function", start, end, f"{target} = {self.src(rhs, self.close(kk))}", (body, end))
                return end + 1
        if self.is_p(rhs, "{"):
            self.object_members(target, rhs, start)
            return self.close(rhs) + 1
        return self.stmt_end(rhs, b)

    def call_statement(self, start: int, i: int, b: int) -> int:
        end_stmt = self.stmt_end(i, b)
        # route: app.get('/path', …)
        if (self.v(i) in _ROUTE_OBJECTS and self.is_p(i + 1, ".") and self.v(i + 2) in _ROUTE_METHODS
                and self.is_p(i + 3, "(") and self.kind(i + 4) == LIT and self.v(i + 4)[1:2] == "/"):
            close = self.close(i + 3)
            method = self.v(i + 2).upper()
            path = self.v(i + 4)[1:-1]
            name = f"{'ROUTE' if method in ('ALL', 'USE') else method} {path}"
            # Handlers passed by name (authMiddleware, users.getUser) are what the route runs.
            handlers = [self.v(x) for x in range(i + 5, close)
                        if self.kind(x) == ID and self.v(x) not in _KEYWORDS and self.v(x + 1) in (",", ")")
                        and self.v(x - 1) in (",", ".")]
            self.add(name, "route", start, close, self.src(i, i + 4) + ", …)", (i + 5, close), extra_refs=handlers)
            return max(end_stmt, close + 1)
        # test block: describe('…', () => { … })
        if (self.v(i) in _TEST_CALLS or (self.v(i) in _TEST_CALLS and self.is_p(i + 1, "."))) and self.is_p(i + 1, "(") \
                and self.kind(i + 2) == LIT:
            close = self.close(i + 1)
            label = self.v(i + 2).strip("'\"`")[:60] or self.v(i)
            self.add(f"{self.v(i)}: {label}", "test", start, close, f"{self.v(i)}({self.v(i + 2)[:62]}, …)", (i + 3, close))
            return max(end_stmt, close + 1)
        return end_stmt

    # classes and objects -----------------------------------------------------
    def class_decl(self, start: int, i: int, b: int, name: str | None = None) -> int:
        k = i + 1
        if self.kind(k) == ID and self.v(k) not in ("extends", "implements"):
            name = name or self.v(k)
            k += 1
        name = name or self.default_name()
        k = self.skip_generic(k)                          # class Context<E = any, I extends Input = {}> {
        brace = next((x for x in range(k, min(b, k + 120)) if self.is_p(x, "{") and not self._type_brace(x)), None)
        if brace is None:
            return self.stmt_end(i, b)
        bases = []
        mode = None
        for x in range(k, brace):
            if self.v(x) in ("extends", "implements"):
                mode = self.v(x)
            elif mode and self.kind(x) == ID and self.v(x)[:1].isupper():
                bases.append(self.v(x))
        end = self.close(brace)
        sig = self.src(i, brace - 1)
        self.add(name, "class", start, end, sig, None, extra_refs=bases)
        self.class_members(name, brace + 1, end)
        return end + 1

    def class_members(self, owner: str, a: int, b: int) -> None:
        i = a
        while i < b:
            start = i
            while self.is_p(i, "@"):
                i = self._skip_decorator(i)
            mods = []
            while self.kind(i) == ID and self.v(i) in _MODIFIERS and not (self.is_p(i + 1, "(") or self.is_p(i + 1, "=")
                                                                       or self.is_p(i + 1, ":") or self.is_p(i + 1, ";")):
                mods.append(self.v(i))
                i += 1
            accessor = None
            if self.kind(i) == ID and self.v(i) in ("get", "set") and (self.kind(i + 1) in (ID, LIT) or self.is_p(i + 1, "[")
                                                                       or self.is_p(i + 1, "#")):
                accessor = self.v(i)
                i += 1
            if self.is_p(i, "*"):
                i += 1
            if self.is_p(i, ";") or self.is_p(i, ","):
                i += 1
                continue
            # member name
            if self.is_p(i, "#"):
                i += 1
            if self.is_p(i, "["):
                name = None                              # computed key or index signature
                i = self.close(i) + 1
            elif self.kind(i) in (ID, LIT):
                name = self.v(i).strip("'\"")
                name_at = i
                i += 1
            else:
                i = max(i + 1, self.stmt_end(i, b))
                continue
            if self.is_p(i, "?") or self.is_p(i, "!"):
                i += 1
            gen = self.skip_generic(i)
            if self.is_p(gen, "("):
                parts = self.function_parts(gen, b)
                if parts and name:
                    body, end = parts
                    sig = " ".join(mods + ([accessor] if accessor else []) + [self.src(name_at, self.close(gen))])
                    self.add(f"{owner}.{name}", "method", start, end, sig, (body, end))
                    i = end + 1
                else:                                    # abstract method / overload: no body
                    i = self.stmt_end(gen, b)
                continue
            if self.is_p(i, ":"):
                i = self.skip_type(i + 1, ("=", ";"))
            if self.is_p(i, "=") and name:
                arrow = self.arrow_at(i + 1)
                if arrow is not None:
                    end = self.body_end(arrow + 1, b)
                    self.add(f"{owner}.{name}", "method", start, end, f"{name} = {self.src(i + 1, arrow)}", (arrow + 1, end + 1))
                    i = end + 1
                    continue
                # name = wrapper(async (…) => { … }), e.g. withBatchedUpdates / debounce / useCallback
                k = i + 1
                while self.kind(k) == ID and self.is_p(k + 1, "."):
                    k += 2
                call = self.skip_generic(k + 1) if self.kind(k) == ID else -1
                if call > 0 and self.is_p(call, "("):
                    close = self.close(call)
                    if any(self.v(x) in ("=>", "function") for x in range(call, close)):
                        end = self.tail_end(close, b)
                        self.add(f"{owner}.{name}", "method", start, end, f"{name} = {self.src(i + 1, k)}(…)",
                                 (call, close + 1), extra_refs=[self.v(k)])
                        i = end + 1
                        continue
            if self.is_p(i, ";"):
                i += 1
            elif i < b and self.line(i) > self.line(i - 1) and not self.is_p(i, "="):
                continue                                 # "timeout: number" with no ";": the next member starts here
            else:
                i = max(i + 1, self.stmt_end(i, b))

    def object_members(self, owner: str, brace: int, start: int) -> int:
        """Function-valued members of an object literal at ``brace``, as ``owner.member`` methods."""
        end = self.close(brace)
        i = brace + 1
        found = 0
        while i < end:
            item_end = i
            while item_end < end and not self.is_p(item_end, ","):
                if self.kind(item_end) == P and self.v(item_end) in "([{":
                    item_end = self.close(item_end)
                item_end += 1
            k = i
            if self.v(k) == "async" and self.kind(k + 1) in (ID, LIT):
                k += 1
            if self.is_p(k, "*"):
                k += 1
            if self.kind(k) in (ID, LIT):
                name = self.v(k).strip("'\"")
                if self.is_p(k + 1, "(") or self.is_p(k + 1, "<"):
                    parts = self.function_parts(self.skip_generic(k + 1), end)
                    if parts:
                        body, b_end = parts
                        self.add(f"{owner}.{name}", "method", i, b_end, self.src(i, self.close(self.skip_generic(k + 1))), (body, b_end))
                        found += 1
                elif self.is_p(k + 1, ":"):
                    vstart = k + 2
                    arrow = self.arrow_at(vstart)
                    if arrow is not None:
                        b_end = min(self.body_end(arrow + 1, item_end), item_end - 1)
                        self.add(f"{owner}.{name}", "method", i, b_end, f"{name}: {self.src(vstart, arrow)}", (arrow + 1, b_end + 1))
                        found += 1
                    elif self.v(vstart) == "function" or (self.v(vstart) == "async" and self.v(vstart + 1) == "function"):
                        kk = vstart + (2 if self.v(vstart) == "async" else 1)
                        if self.kind(kk) == ID:
                            kk += 1
                        parts = self.function_parts(kk, end)
                        if parts:
                            body, b_end = parts
                            self.add(f"{owner}.{name}", "method", i, b_end, f"{name}: {self.src(vstart, self.close(kk))}", (body, b_end))
                            found += 1
                    elif self.is_p(vstart, "{") and owner == self.default_name() and name in ("methods", "computed", "watch"):
                        found += self.object_members(owner, vstart, i)   # Vue options API: methods: { … }
            i = item_end + 1
        return found


# --------------------------------------------------------------------- entry
def extract(path: str, text: str, language: str) -> FileExtraction:
    """Symbols for a JavaScript/TypeScript file (or the <script> blocks of a Vue/Svelte file)."""
    lines = text.count("\n") + 1
    if len(text) > MAX_FILE_CHARS or len(text) / lines > MAX_AVG_LINE_CHARS:
        raise ScanError("too large or minified")
    if language in ("vue", "svelte"):
        out = FileExtraction()
        for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", text, re.S | re.I):
            offset = text.count("\n", 0, m.start(2))
            sub = _extract(path, m.group(2), jsx="tsx" in m.group(1) or "jsx" in m.group(1))
            for s in sub.symbols:
                s.line += offset
                if s.end_line:
                    s.end_line += offset
            out.symbols.extend(sub.symbols)
        return out
    suffix = PurePosixPath(path).suffix.lower()
    return _extract(path, text, jsx=suffix not in (".ts", ".mts", ".cts", ".d.ts"))


def _extract(path: str, text: str, jsx: bool) -> FileExtraction:
    try:
        tokens, comments = tokenize(text, jsx)
    except ScanError:
        if not jsx:
            raise
        tokens, comments = tokenize(text, jsx=False)      # a ".js" file with no JSX but stray "<"s
    parser = _Parser(path, text, tokens, comments)
    parser.scan_block(0, parser.n)
    out = FileExtraction()
    seen: set[tuple[str, int]] = set()
    for s in parser.symbols:
        if (s.name, s.line) not in seen:
            seen.add((s.name, s.line))
            out.symbols.append(s)
    return out
