"""PSQL parser and trace instrumentation.

Firebird has no debugger API, so a "trace run" executes an instrumented copy of the routine as an
``EXECUTE BLOCK``. The copy

* declares the routine's input parameters as local variables initialised with the given values,
* keeps the routine's output parameters (a function's result becomes the column ``FBT$R``),
* copies the declarations (variables, cursors, sub-routines) and the body verbatim, and
* inserts, after each executable statement, a tracing statement that sets ``FBT$K`` (step kind),
  ``FBT$L`` (line in the original source) and one ``FBT$<n>`` column per traced variable, then
  ``SUSPEND``s the row to the client.

Every insertion is made on the line of the statement it belongs to and the generated header shares
the first line of the source, so a line number in the instrumented block (in an error message, say)
*is* the line number in the original source.

Shapes that change meaning when a statement is added after them are rewritten:
``IF (c) THEN stmt;`` becomes ``IF (c) THEN BEGIN stmt; <trace> END`` (same for ELSE, WHILE ... DO,
FOR ... DO and WHEN ... DO), ``RETURN expr;`` in a function becomes ``FBT$R = expr; <result row>
EXIT;``, and ``EXIT`` emits the final row first. Statements are never removed or reordered.

What cannot be handled safely is refused with :class:`PsqlError` (``IN AUTONOMOUS TRANSACTION``,
``EXECUTE STATEMENT ... ON EXTERNAL``, text the parser does not understand).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..metadata.types import quote_ident, sql_string

# Step kinds (column FBT$K)
ENTRY, STEP, ROW, FINAL, HANDLER, FETCH, ERROR = 0, 1, 2, 3, 4, 5, 9
KIND_NAMES = {ENTRY: "entry", STEP: "step", ROW: "row", FINAL: "end", HANDLER: "handler", FETCH: "fetch",
              ERROR: "error"}

_Q_CLOSERS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_OPS2 = ("||", "<>", "!=", "^=", "~=", "<=", ">=", "!<", "!>", "^<", "^>", "~<", "~>")
BUILTIN_TYPES = frozenset(
    "SMALLINT INTEGER INT BIGINT INT128 FLOAT REAL DOUBLE DECFLOAT NUMERIC DECIMAL DEC DATE TIME TIMESTAMP "
    "CHAR CHARACTER VARCHAR NCHAR NATIONAL BLOB BOOLEAN BINARY VARBINARY LONG TYPE".split()
)


class PsqlError(ValueError):
    """The source cannot be parsed or instrumented safely."""

    def __init__(self, message: str, line: int | None = None, code: str = "unsupported"):
        super().__init__(message if line is None else f"Line {line}: {message}")
        self.line = line
        self.code = code


# ------------------------------------------------------------------ tokens
@dataclass
class Tok:
    kind: str  # word, qid, str, num, op
    text: str
    start: int
    end: int
    line: int

    @property
    def up(self) -> str:
        return self.text.upper() if self.kind == "word" else ""

    @property
    def name(self) -> str:
        """Identifier as Firebird stores it (upper case unless quoted)."""
        if self.kind == "qid":
            return self.text[1:-1].replace('""', '"')
        return self.text.upper()


def tokenize(src: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n, line = 0, len(src), 1

    def add(kind: str, j: int) -> None:
        nonlocal line
        toks.append(Tok(kind, src[i:j], i, j, line))
        line += src.count("\n", i, j)

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
            i += 1
        elif ch.isspace():
            i += 1
        elif ch == "-" and nxt == "-":
            j = src.find("\n", i)
            i = n if j == -1 else j
        elif ch == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            if j == -1:
                raise PsqlError("unterminated /* comment", line)
            line += src.count("\n", i, j + 2)
            i = j + 2
        elif ch in "qQ" and nxt == "'" and i + 2 < n:
            closer = _Q_CLOSERS.get(src[i + 2], src[i + 2])
            j = src.find(closer + "'", i + 3)
            if j == -1:
                raise PsqlError("unterminated q'…' string", line)
            add("str", j + 2)
            i = j + 2
        elif ch in ("'", '"'):
            j = i + 1
            while True:
                if j >= n:
                    raise PsqlError("unterminated string" if ch == "'" else "unterminated quoted name", line)
                if src[j] == ch:
                    if j + 1 < n and src[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            add("str" if ch == "'" else "qid", j + 1)
            i = j + 1
        elif ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            add("word", j)
            i = j
        elif ch.isdigit() or (ch == "." and nxt.isdigit()):
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] == "." or (src[j] in "+-" and src[j - 1] in "eE")):
                j += 1
            add("num", j)
            i = j
        else:
            j = i + 2 if src[i:i + 2] in _OPS2 else i + 1
            add("op", j)
            i = j
    return toks


# ------------------------------------------------------------------ syntax tree
@dataclass
class Node:
    kind: str  # simple, block, if, while, for
    a: int  # first token (the label for labelled loops)
    b: int  # last token
    line: int
    kw: str = ""  # first keyword of a simple statement
    stmts: list[Node] = field(default_factory=list)  # block
    handlers: list[Handler] = field(default_factory=list)  # block
    then: Node | None = None  # if
    els: Node | None = None  # if
    body: Node | None = None  # while / for
    head: int = 0  # token index of THEN / DO


@dataclass
class Handler:
    a: int  # WHEN
    do: int
    body: Node
    line: int


@dataclass
class Var:
    name: str  # as stored (upper case unless quoted)
    type: str  # declared type text, without NOT NULL / COLLATE / default
    kind: str = "local"  # input, output, local
    not_null: bool = False
    collate: str | None = None

    @property
    def decl(self) -> str:
        """Type with NOT NULL and COLLATE (for declarations)."""
        return self.type + (" NOT NULL" if self.not_null else "") + (f" COLLATE {self.collate}" if self.collate else "")

    @property
    def ref(self) -> str:
        return quote_ident(self.name)


@dataclass
class Parsed:
    source: str
    toks: list[Tok]
    locals: list[Var]
    cursors: list[str]
    subroutines: list[str]
    main: Node
    # EXECUTE BLOCK only
    inputs: list[Var] = field(default_factory=list)
    outputs: list[Var] = field(default_factory=list)
    body_start: int = 0  # character offset of the text after AS (0 for stored routine sources)

    def has_suspend(self) -> bool:
        return any(t.up == "SUSPEND" for t in self.toks[self.main.a:self.main.b + 1])

    def line_text(self, line: int) -> str:
        lines = self.source.split("\n")
        return lines[line - 1].strip() if 0 < line <= len(lines) else ""


class _Parser:
    def __init__(self, src: str, toks: list[Tok]):
        self.src = src
        self.t = toks
        self.n = len(toks)

    # helpers
    def tok(self, i: int) -> Tok:
        if i >= self.n:
            last = self.t[-1].line if self.t else 1
            raise PsqlError("unexpected end of the source (missing END?)", last, "syntax")
        return self.t[i]

    def is_op(self, i: int, op: str) -> bool:
        return i < self.n and self.t[i].kind == "op" and self.t[i].text == op

    def expect_word(self, i: int, word: str) -> None:
        if self.tok(i).up != word:
            raise PsqlError(f"expected {word}, found {self.t[i].text!r}", self.t[i].line, "syntax")

    def match_paren(self, i: int) -> int:
        if not self.is_op(i, "("):
            raise PsqlError(f"expected '(', found {self.tok(i).text!r}", self.tok(i).line, "syntax")
        depth = 0
        while True:
            t = self.tok(i)
            if t.kind == "op" and t.text == "(":
                depth += 1
            elif t.kind == "op" and t.text == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1

    def find_top(self, i: int, word: str) -> int:
        """Index of ``word`` at parenthesis depth 0 (before any top-level ``;``)."""
        depth = 0
        start = i
        while True:
            t = self.tok(i)
            if t.kind == "op" and t.text == "(":
                depth += 1
            elif t.kind == "op" and t.text == ")":
                depth -= 1
            elif depth == 0 and t.up == word:
                return i
            elif depth == 0 and t.kind == "op" and t.text == ";":
                raise PsqlError(f"expected {word} before ';'", self.t[start].line, "syntax")
            i += 1

    def semicolon(self, i: int) -> int:
        """Index of the ``;`` ending the statement that starts at ``i``."""
        depth = 0
        case = 0
        while True:
            t = self.tok(i)
            if t.kind == "op":
                if t.text == "(":
                    depth += 1
                elif t.text == ")":
                    depth -= 1
                elif t.text == ";" and depth == 0:
                    return i
            elif t.up == "CASE":
                case += 1
            elif t.up == "END" and depth == 0:
                if not case:
                    raise PsqlError("missing ';' before END", t.line, "syntax")
                case -= 1
            elif t.up == "BEGIN" and depth == 0:
                raise PsqlError("missing ';' before BEGIN", t.line, "syntax")
            i += 1

    # declarations
    def declarations(self, i: int, collect: bool, out: Parsed | None = None) -> int:
        while i < self.n and self.t[i].up == "DECLARE":
            j = i + 1
            if self.tok(j).up in ("PROCEDURE", "FUNCTION"):
                name = self.tok(j + 1).name
                k = j + 2
                depth = 0
                while True:
                    t = self.tok(k)
                    if t.kind == "op" and t.text == "(":
                        depth += 1
                    elif t.kind == "op" and t.text == ")":
                        depth -= 1
                    elif depth == 0 and t.kind == "op" and t.text == ";":
                        end = k  # forward declaration
                        break
                    elif depth == 0 and t.up == "AS":
                        k2 = self.declarations(k + 1, collect=False)
                        self.expect_word(k2, "BEGIN")
                        blk = self.block(k2)
                        end = blk.b
                        if self.is_op(end + 1, ";"):
                            end += 1
                        break
                    k += 1
                if out is not None and collect:
                    out.subroutines.append(name)
                i = end + 1
                continue
            if self.tok(j).up == "VARIABLE":
                j += 1
            name_tok = self.tok(j)
            if name_tok.kind not in ("word", "qid"):
                raise PsqlError(f"expected a variable name after DECLARE, found {name_tok.text!r}", name_tok.line, "syntax")
            k = j + 1
            if self.tok(k).up in ("CURSOR", "SCROLL", "NO"):
                end = self.semicolon(k)
                if out is not None and collect:
                    out.cursors.append(name_tok.name)
                i = end + 1
                continue
            depth = 0
            m = k
            not_null = False
            last = k
            while True:
                t = self.tok(m)
                if t.kind == "op" and t.text == "(":
                    depth += 1
                elif t.kind == "op" and t.text == ")":
                    depth -= 1
                elif depth == 0 and (t.up in ("NOT", "COLLATE", "DEFAULT") or (t.kind == "op" and t.text in ("=", ";"))):
                    break
                last = m
                m += 1
            if m == k:
                raise PsqlError(f"variable {name_tok.text} has no type", name_tok.line, "syntax")
            end = self.semicolon(m)
            if any(self.t[x].up == "NOT" and self.t[x + 1].up == "NULL" for x in range(m, end)):
                not_null = True
            if out is not None and collect:
                out.locals.append(Var(name_tok.name, self.src[self.t[k].start:self.t[last].end], "local", not_null))
            i = end + 1
        return i

    # statements
    def block(self, i: int) -> Node:
        self.expect_word(i, "BEGIN")
        node = Node("block", i, i, self.t[i].line)
        j = i + 1
        while True:
            t = self.tok(j)
            if t.up == "END":
                node.b = j
                return node
            if t.up == "WHEN":
                do = self.find_top(j + 1, "DO")
                body = self.stmt(do + 1)
                node.handlers.append(Handler(j, do, body, t.line))
                j = body.b + 1
                continue
            if node.handlers:
                raise PsqlError("statements after a WHEN handler", t.line, "syntax")
            s = self.stmt(j)
            node.stmts.append(s)
            j = s.b + 1

    def stmt(self, i: int) -> Node:
        t = self.tok(i)
        if t.kind in ("word", "qid") and self.is_op(i + 1, ":") and t.up not in ("BEGIN", "END"):
            inner = self.stmt(i + 2)
            if inner.kind not in ("while", "for"):
                raise PsqlError("a label must be followed by FOR or WHILE", t.line, "syntax")
            inner.a = i
            inner.line = t.line
            return inner
        w = t.up
        if w == "BEGIN":
            return self.block(i)
        if w == "IF":
            th = self.find_top(i + 1, "THEN")
            then = self.stmt(th + 1)
            node = Node("if", i, then.b, t.line, then=then, head=th)
            if then.b + 1 < self.n and self.t[then.b + 1].up == "ELSE":
                node.els = self.stmt(then.b + 2)
                node.b = node.els.b
            return node
        if w == "WHILE":
            do = self.find_top(i + 1, "DO")
            body = self.stmt(do + 1)
            return Node("while", i, body.b, t.line, body=body, head=do)
        if w == "FOR":
            do = self.find_top(i + 1, "DO")
            body = self.stmt(do + 1)
            return Node("for", i, body.b, t.line, body=body, head=do)
        if w == "IN" and self.tok(i + 1).up == "AUTONOMOUS":
            raise PsqlError("IN AUTONOMOUS TRANSACTION commits its own changes, which a trace run could not undo; "
                            "it is not supported", t.line, "autonomous")
        if w in ("WHEN", "END", "ELSE", "THEN", "DO", "DECLARE"):
            raise PsqlError(f"unexpected {t.text}", t.line, "syntax")
        if t.kind == "op" and t.text == ";":
            raise PsqlError("empty statement (';' alone)", t.line, "syntax")
        end = self.semicolon(i)
        return Node("simple", i, end, t.line, kw=w)


def check_side_effects(toks: list[Tok]) -> list[str]:
    """Refuse what a rollback cannot undo; return warnings for the rest."""
    warnings: list[str] = []
    seen: set[str] = set()

    def warn(key: str, msg: str) -> None:
        if key not in seen:
            seen.add(key)
            warnings.append(msg)

    for k, t in enumerate(toks):
        w = t.up
        prev = toks[k - 1].up if k else ""
        nxt = toks[k + 1].up if k + 1 < len(toks) else ""
        if w == "AUTONOMOUS" and prev in ("IN", "WITH"):
            raise PsqlError(f"{prev} AUTONOMOUS TRANSACTION commits its own changes, which a trace run could not "
                            "undo; it is not supported", t.line, "autonomous")
        if w == "ON" and nxt == "EXTERNAL":
            raise PsqlError("EXECUTE STATEMENT ... ON EXTERNAL runs in another connection whose changes a trace "
                            "run could not undo; it is not supported", t.line, "external")
        if w == "GEN_ID" or (w == "NEXT" and nxt == "VALUE"):
            warn("seq", "The routine uses sequences (GEN_ID / NEXT VALUE FOR): sequence increments are not "
                        "transactional and are NOT rolled back.")
        if w == "POST_EVENT":
            warn("event", "POST_EVENT: events are only delivered on commit, so the trace run does not deliver them.")
        if w == "EXECUTE" and nxt == "STATEMENT":
            warn("es", "EXECUTE STATEMENT runs dynamic SQL; it is executed (and rolled back) but not traced.")
    return warnings


def parse_source(source: str) -> Parsed:
    """Parse a stored routine source (``RDB$PROCEDURE_SOURCE``: declarations + ``BEGIN ... END``)."""
    toks = tokenize(source)
    return _parse_body(source, toks, 0)


def _parse_body(source: str, toks: list[Tok], i: int) -> Parsed:
    p = _Parser(source, toks)
    if i >= len(toks):
        raise PsqlError("the routine has no PSQL body", 1, "syntax")
    out = Parsed(source, toks, [], [], [], Node("block", 0, 0, 1))
    i = p.declarations(i, collect=True, out=out)
    if i >= len(toks) or toks[i].up != "BEGIN":
        t = toks[i] if i < len(toks) else toks[-1]
        raise PsqlError(f"expected BEGIN, found {t.text!r}", t.line, "syntax")
    out.main = p.block(i)
    rest = [t for t in toks[out.main.b + 1:] if not (t.kind == "op" and t.text in (";", "^", "!!"))]
    if rest:
        raise PsqlError(f"unexpected {rest[0].text!r} after the final END", rest[0].line, "syntax")
    return out


_EB_HEAD = re.compile(r"^\s*EXECUTE\s+BLOCK\b", re.IGNORECASE)


def is_execute_block(sql: str) -> bool:
    from ..sqlsplit import strip_comments

    return bool(_EB_HEAD.match(strip_comments(sql)))


def _param_list(p: _Parser, open_i: int, kind: str) -> tuple[list[Var], int]:
    close = p.match_paren(open_i)
    out: list[Var] = []
    i = open_i + 1
    while i < close:
        name = p.tok(i)
        if name.kind not in ("word", "qid"):
            raise PsqlError(f"expected a parameter name, found {name.text!r}", name.line, "syntax")
        m = i + 1
        depth = 0
        while m < close and not (depth == 0 and p.t[m].kind == "op" and p.t[m].text == ","):
            if p.t[m].kind == "op" and p.t[m].text == "(":
                depth += 1
            elif p.t[m].kind == "op" and p.t[m].text == ")":
                depth -= 1
            m += 1
        if m == i + 1:
            raise PsqlError(f"parameter {name.text} has no type", name.line, "syntax")
        out.append(var_from_decl(name.name, p.src[p.t[i + 1].start:p.t[m - 1].end], kind))
        i = m + 1
    return out, close


def parse_execute_block(sql: str) -> Parsed:
    """Parse ``EXECUTE BLOCK [(inputs)] [RETURNS (outputs)] AS ...``."""
    sql = sql.rstrip().rstrip(";").rstrip()
    toks = tokenize(sql)
    p = _Parser(sql, toks)
    if len(toks) < 2 or toks[0].up != "EXECUTE" or toks[1].up != "BLOCK":
        raise PsqlError("not an EXECUTE BLOCK statement", 1, "syntax")
    i = 2
    inputs: list[Var] = []
    outputs: list[Var] = []
    if p.is_op(i, "("):
        inputs, close = _param_list(p, i, "input")
        i = close + 1
    if p.tok(i).up == "RETURNS":
        outputs, close = _param_list(p, i + 1, "output")
        i = close + 1
    p.expect_word(i, "AS")
    parsed = _parse_body(sql, toks, i + 1)
    parsed.inputs = inputs
    parsed.outputs = outputs
    parsed.body_start = toks[i].end
    return parsed


# ------------------------------------------------------------------ instrumentation
_NOT_NULL = re.compile(r"\s+NOT\s+NULL\b", re.IGNORECASE)
_COLLATE = re.compile(r"\s+COLLATE\s+\S+", re.IGNORECASE)
_DEFAULT = re.compile(r"\s*(=|\bDEFAULT\b).*$", re.IGNORECASE | re.DOTALL)


def bare_type(type_text: str) -> str:
    """Type without NOT NULL, COLLATE and a default value."""
    t = _DEFAULT.sub("", type_text.strip())
    return _COLLATE.sub("", _NOT_NULL.sub("", t)).strip()


def column_type(type_text: str) -> str:
    """A nullable type for a trace column: domains become ``TYPE OF <domain>`` (no NOT NULL / CHECK)."""
    t = bare_type(type_text)
    first = re.match(r'\s*("(?:[^"]|"")+"|[\w$]+)', t)
    word = first.group(1) if first else ""
    if word and not word.startswith('"') and word.upper() in BUILTIN_TYPES:
        return t
    return "TYPE OF " + t


_COLLATE_NAME = re.compile(r"\bCOLLATE\s+(\S+)", re.IGNORECASE)


def var_from_decl(name: str, type_text: str, kind: str) -> Var:
    """``Var`` from a declaration such as ``VARCHAR(10) NOT NULL COLLATE UNICODE_CI = 'x'``."""
    no_default = _DEFAULT.sub("", type_text.strip())
    m = _COLLATE_NAME.search(no_default)
    return Var(name, bare_type(type_text), kind, bool(_NOT_NULL.search(no_default)), m.group(1) if m else None)


@dataclass
class TraceVar:
    name: str
    kind: str  # input, local
    type: str
    column: str | None = None  # FBT$<n>, None when not traced
    mode: str = "value"  # value | size (BLOB: octet length) | text (long string: first N characters)
    col_type: str = ""
    text_chars: int = 1000

    def expr(self) -> str:
        ref = quote_ident(self.name)
        if self.mode == "size":
            return f"OCTET_LENGTH({ref})"
        if self.mode == "text":
            return f"SUBSTRING({ref} FROM 1 FOR {self.text_chars})"
        return ref


@dataclass
class Instrumented:
    sql: str
    columns: list[tuple[str, str]]  # (role, name): role in kind, line, output, result, gds, state, message, var
    variables: list[TraceVar]
    outputs: list[Var]
    kind: str  # selectable, executable, function
    warnings: list[str]
    line_count: int


class _Instrumenter:
    def __init__(self, parsed: Parsed, kind: str, tvars: list[TraceVar], with_message: bool,
                 catch_errors: bool = False):
        self.catch_errors = catch_errors
        self.p = parsed
        self.t = parsed.toks
        self.kind = kind
        self.tvars = [v for v in tvars if v.column]
        self.by_name = {v.name: v for v in self.tvars}
        self.edits: list[tuple[int, int, int, str]] = []  # (start, seq, end, text)
        self.with_message = with_message

    def ins(self, pos: int, text: str, end: int | None = None) -> None:
        self.edits.append((pos, len(self.edits), pos if end is None else end, text))

    def mentioned(self, a: int, b: int) -> list[str]:
        names = {t.name for t in self.t[a:b + 1] if t.kind in ("word", "qid")}
        return [v.name for v in self.tvars if v.name in names]

    def trace(self, k: int, line: int, names: list[str] | None) -> str:
        parts = [f"FBT$K={k};FBT$L={line};"]
        for v in self.tvars if names is None else [self.by_name[n] for n in names]:
            parts.append(f"{v.column}={v.expr()};")
        parts.append("SUSPEND;")
        return " " + "".join(parts) + " "

    def handler_trace(self, line: int) -> str:
        err = "FBT$G=GDSCODE;FBT$S=SQLSTATE;" + ("FBT$M=RDB$ERROR(MESSAGE);" if self.with_message else "")
        return " " + err + self.trace(HANDLER, line, None).strip() + " "

    def final(self, line: int) -> str:
        return self.trace(FINAL, line, None)

    # --- nodes
    def stmt(self, n: Node, in_list: bool) -> None:
        if not in_list and n.kind != "block":
            self.ins(self.t[n.a].start, "BEGIN ")
            self.stmt(n, True)
            self.ins(self.t[n.b].end, " END")
            return
        if n.kind == "block":
            self.block(n)
        elif n.kind == "if":
            self.stmt(n.then, False)  # type: ignore[arg-type]
            if n.els is not None:
                self.stmt(n.els, False)
        elif n.kind == "while":
            self.stmt(n.body, False)  # type: ignore[arg-type]
        elif n.kind == "for":
            self.prefixed(n.body, self.trace(FETCH, n.line, self.mentioned(n.a, n.head)))  # type: ignore[arg-type]
        else:
            self.simple(n)

    def prefixed(self, body: Node, prefix: str) -> None:
        if body.kind == "block":
            self.ins(self.t[body.a].end, prefix)
            self.block(body)
        else:
            self.ins(self.t[body.a].start, "BEGIN" + prefix)
            self.stmt(body, True)
            self.ins(self.t[body.b].end, " END")

    def block(self, n: Node) -> None:
        for s in n.stmts:
            self.stmt(s, True)
        for h in n.handlers:
            self.prefixed(h.body, self.handler_trace(h.line))

    def simple(self, n: Node) -> None:
        a, b = self.t[n.a], self.t[n.b]
        if n.kw == "SUSPEND":
            if self.kind == "function":
                raise PsqlError("SUSPEND is not allowed in a function", n.line, "syntax")
            self.ins(a.start, f"FBT$K={ROW};FBT$L={n.line}; ")
        elif n.kw == "EXIT":
            self.ins(a.start, self.final(n.line).lstrip())
        elif n.kw == "RETURN":
            if self.kind != "function":
                raise PsqlError("RETURN is only allowed in a function", n.line, "syntax")
            self.ins(a.start, "FBT$R =", a.end)
            self.ins(b.end, self.final(n.line) + "EXIT;")
        elif n.kw in ("LEAVE", "BREAK", "CONTINUE", "EXCEPTION"):
            self.ins(a.start, self.trace(STEP, n.line, []).lstrip())
        else:
            self.ins(b.end, self.trace(STEP, n.line, self.mentioned(n.a, n.b)))

    def run(self) -> str:
        main = self.p.main
        begin, end = self.t[main.a], self.t[main.b]
        self.ins(begin.start, "BEGIN BEGIN " if self.catch_errors else "BEGIN ")
        self.ins(begin.end, self.trace(ENTRY, main.line, None))
        self.block(main)
        tail = self.final(end.line) + "END"
        if self.catch_errors:
            # report an error that escapes the routine as a row instead of failing the statement, so the
            # client can fetch in batches without losing the rows of the failing batch (FB4+: the
            # message text comes from RDB$ERROR(MESSAGE))
            tail += (" WHEN ANY DO BEGIN FBT$G=GDSCODE;FBT$S=SQLSTATE;FBT$M=RDB$ERROR(MESSAGE);"
                     f"FBT$K={ERROR};FBT$L=NULL;SUSPEND; END END")
        self.ins(end.end, tail)
        src = self.p.source
        out: list[str] = []
        pos = self.p.body_start
        for start, _seq, stop, text in sorted(self.edits):
            out.append(src[pos:start])
            out.append(text)
            pos = max(pos, stop)
        out.append(src[pos:])
        return "".join(out)


def literal(value: str | None) -> str:
    return "NULL" if value is None else sql_string(str(value))


def instrument(
    parsed: Parsed,
    *,
    kind: str,
    inputs: list[Var],
    outputs: list[Var],
    values: dict[str, str | None],
    returns: str | None = None,
    tvars: list[TraceVar] | None = None,
    with_message: bool = False,
    catch_errors: bool = False,
) -> Instrumented:
    """Build the instrumented ``EXECUTE BLOCK``.

    ``kind``: ``selectable`` / ``executable`` procedure (or block) or ``function``; ``inputs`` /
    ``outputs``: the routine's parameters; ``values``: input values (text, ``None`` = NULL);
    ``tvars``: traced variables with their trace column (default: every input and local variable);
    ``with_message``: record ``RDB$ERROR(MESSAGE)`` in handlers (Firebird 4+); ``catch_errors``: turn an
    error escaping the routine into a final ``FBT$K = 9`` row (Firebird 4+).
    """
    if kind not in ("selectable", "executable", "function"):
        raise ValueError(f"unknown routine kind {kind!r}")
    warnings = check_side_effects(parsed.toks)
    if tvars is None:
        tvars = default_trace_vars(parsed, inputs)
    names = {v.name for v in inputs} | {v.name for v in parsed.locals}
    for v in tvars:
        if v.name not in names:
            raise PsqlError(f"unknown variable {v.name}")
    cols: list[tuple[str, str]] = [("kind", "FBT$K"), ("line", "FBT$L")]
    decl: list[str] = ["FBT$K SMALLINT", "FBT$L INTEGER"]
    for o in outputs:
        cols.append(("output", o.name))
        # trace rows are SUSPENDed before the outputs are set: Firebird would reject a NULL in a NOT NULL
        # output (or a domain with NOT NULL), so output columns are nullable copies of the declared type
        decl.append(f"{quote_ident(o.name)} {column_type(o.type)}" + (f" COLLATE {o.collate}" if o.collate else ""))
        if o.not_null or column_type(o.type).startswith("TYPE OF ") and not o.type.upper().startswith("TYPE OF"):
            warnings.append(f"Output {o.name}: NOT NULL and domain CHECK constraints of output parameters are "
                            "not enforced in a trace run.")
    if kind == "function":
        cols.append(("result", "FBT$R"))
        decl.append(f"FBT$R {column_type(returns or 'VARCHAR(8191)')}")
    cols += [("gds", "FBT$G"), ("state", "FBT$S")]
    decl += ["FBT$G INTEGER", "FBT$S VARCHAR(5)"]
    if with_message:
        cols.append(("message", "FBT$M"))
        decl.append("FBT$M VARCHAR(1024) CHARACTER SET UTF8")
    for v in tvars:
        if v.column:
            cols.append(("var", v.name))
            decl.append(f"{v.column} {v.col_type or column_type(v.type)}")
    for name in values:
        if name not in {v.name for v in inputs}:
            raise PsqlError(f"unknown input parameter {name}")
    head = "EXECUTE BLOCK RETURNS (" + ", ".join(decl) + ") AS "
    for v in inputs:
        head += f"DECLARE {v.ref} {v.decl} = {literal(values.get(v.name))}; "
    if catch_errors and not with_message:
        raise ValueError("catch_errors needs with_message (RDB$ERROR, Firebird 4+)")
    body = _Instrumenter(parsed, kind, tvars, with_message, catch_errors).run()
    # keep line numbers: the header shares the first line of the body (the newlines of an
    # EXECUTE BLOCK header are kept in front of the body)
    header_newlines = parsed.source.count("\n", 0, parsed.body_start)
    sql = head + "\n" * header_newlines + body
    return Instrumented(sql, cols, [v for v in tvars if v.column], outputs, kind, warnings,
                        parsed.source.count("\n") + 1)


def default_trace_vars(parsed: Parsed, inputs: list[Var], watch: set[str] | None = None) -> list[TraceVar]:
    out: list[TraceVar] = []
    for v in [*inputs, *parsed.locals]:
        tv = TraceVar(v.name, "input" if v in inputs else "local", v.type)
        if watch is None or v.name in watch:
            tv.column = f"FBT${len([x for x in out if x.column]) + 1}"
            tv.col_type = column_type(v.type)
        out.append(tv)
    return out
