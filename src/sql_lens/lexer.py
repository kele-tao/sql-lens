"""A hand-written SQL tokenizer built on one master regular expression.

Why a master regex instead of a character loop: Python call overhead dominates a
per-character scanner. One compiled pattern with named alternatives runs the
scan inside the C-level regex engine, and only the matched spans are touched in
Python - roughly 4x faster on multi-megabyte files with exactly the same tokens
and positions.

The lexer keeps three things the rest of the tool needs:

* exact source positions (line/column) so findings point at real code,
* whether the whitespace before a token contained a newline,
* comments as tokens so the formatter can preserve them while the parser skips
  them.
"""

from __future__ import annotations

import re

from .dialects import Dialect, get_dialect

WORD = "WORD"
QUOTED = "QUOTED"
NUMBER = "NUMBER"
STRING = "STRING"
OP = "OP"
PUNCT = "PUNCT"
COMMENT = "COMMENT"
PLACEHOLDER = "PLACEHOLDER"
EOF = "EOF"

PUNCTUATION = set("(),;.*[]{}")

_THREE_OPS = ("->>", "#>>")
_TWO_OPS = ("->", "#>", "::", ":=", "<=", ">=", "<>", "!=", "||", "<<", ">>", "!~", "~*")
_SINGLE_OPS = "-+/%<>=!~^&|@?:#"

_OPERATORS = "|".join(
    [re.escape(op) for op in _THREE_OPS + _TWO_OPS]
    + ["[" + "".join(re.escape(ch) for ch in _SINGLE_OPS) + "]"]
)
_NUMBER = r"0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+"
_WORD = r"[^\W\d][\w$]*"


class LexError(ValueError):
    """Raised when the tokenizer cannot make sense of the input."""

    def __init__(self, message: str, line: int, col: int) -> None:
        super().__init__(f"line {line}, column {col}: {message}")
        self.line = line
        self.col = col
        self.detail = message


class Token:
    """One lexical unit with its source position.

    A hand-written slotted class rather than a dataclass: the lexer creates one
    Token per lexical unit, so on a 5 MB file that is about a million objects
    and the object-construction path is worth keeping minimal.
    """

    __slots__ = (
        "col",
        "end",
        "kind",
        "line",
        "newline_before",
        "quoted",
        "raw",
        "space_before",
        "start",
        "up",
        "value",
    )

    def __init__(
        self,
        kind: str,
        value: str,
        line: int,
        col: int,
        quoted: bool = False,
        newline_before: bool = False,
        space_before: bool = False,
        up: str = "",
        raw: str = "",
        start: int = -1,
        end: int = -1,
    ) -> None:
        self.kind = kind
        self.value = value
        self.line = line
        self.col = col
        self.quoted = quoted
        self.newline_before = newline_before
        self.space_before = space_before
        self.up = up
        self.raw = raw
        self.start = start
        self.end = end

    @property
    def upper(self) -> str:
        return self.up or self.value.upper()

    def is_word(self, *words: str) -> bool:
        if self.kind != WORD or self.quoted:
            return False
        wanted = _WORD_SETS.get(words)
        if wanted is None:
            wanted = frozenset(w.upper() for w in words)
            _WORD_SETS[words] = wanted
        return (self.up or self.value.upper()) in wanted

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.kind}, {self.value!r}, {self.line}:{self.col})"


_WORD_SETS: dict[tuple[str, ...], frozenset[str]] = {}
_MASTER_CACHE: dict[tuple[str, tuple[tuple[str, str], ...], bool, bool], re.Pattern[str]] = {}
_KIND_CACHE: dict[str, dict[int, str]] = {}

_KIND_NAMES = {
    "ws": "ws",
    "badcomment": "badcomment",
    "lcomment": COMMENT,
    "bcomment": COMMENT,
    "string": STRING,
    "dbody": STRING,
    "dbody2": STRING,
    "placeholder": PLACEHOLDER,
    "number": NUMBER,
    "word": WORD,
    "op": OP,
    "punct": PUNCT,
}


def _build_master(dialect: Dialect) -> re.Pattern[str]:
    """Compile (and cache) the master token pattern for *dialect*."""
    key = (
        dialect.name,
        dialect.quote_pairs,
        dialect.backslash_escapes,
        dialect.supports_dollar_quote,
    )
    cached = _MASTER_CACHE.get(key)
    if cached is not None:
        return cached

    alternatives = [
        r"(?P<ws>[ \t\r\n\f\v]+)",
        r"(?P<lcomment>--[^\r\n]*)",
        r"(?P<bcomment>/\*.*?\*/)",
        r"(?P<badcomment>/\*)",
    ]
    if dialect.supports_dollar_quote:
        alternatives.append(r"\$(?P<dopen>[A-Za-z_]\w*)\$(?P<dbody>.*?)\$(?P=dopen)\$")
        alternatives.append(r"\$\$(?P<dbody2>.*?)\$\$")

    quoted_parts = []
    for index, (opener, closer) in enumerate(dialect.quote_pairs):
        quoted_parts.append(
            re.escape(opener)
            + f"(?P<q{index}>(?:[^{re.escape(closer)}]|{re.escape(closer)}{re.escape(closer)})*)"
            + re.escape(closer)
        )
    if quoted_parts:
        alternatives.append("(?:" + "|".join(quoted_parts) + ")")

    if dialect.backslash_escapes:
        alternatives.append(r"'(?P<string>(?:[^'\\]|\\.|'')*)'")
    else:
        alternatives.append(r"'(?P<string>(?:[^']|'')*)'")

    alternatives.append(r"(?P<placeholder>\$\d+|\?|:[A-Za-z_]\w*|%s)")
    alternatives.append(r"(?P<number>" + _NUMBER + r")")
    alternatives.append(r"(?P<word>" + _WORD + r")")
    alternatives.append(r"(?P<op>" + _OPERATORS + r")")

    punct_chars = "".join(
        ch for ch in "(),;.*[]{}" if ch not in {opener for opener, _ in dialect.quote_pairs}
    )
    alternatives.append(r"(?P<punct>[" + re.escape(punct_chars) + r"])")

    pattern = re.compile("|".join(alternatives), re.DOTALL)
    _MASTER_CACHE[key] = pattern
    return pattern


def _kind_by_index(dialect: Dialect, master: re.Pattern[str]) -> dict[int, str]:
    """Map regex group indexes onto token kinds.

    Cached per dialect so the hot loop does one dict lookup per token instead of
    calling match.group() for every alternative.
    """
    cached = _KIND_CACHE.get(dialect.name)
    if cached is not None:
        return cached
    kinds: dict[int, str] = {}
    for name, index in master.groupindex.items():
        if name in _KIND_NAMES or name.startswith("q"):
            kinds[index] = name
    _KIND_CACHE[dialect.name] = kinds
    return kinds


def _quote_lookup(dialect: Dialect, master: re.Pattern[str]) -> dict[str, str]:
    """Map quoted-identifier group names onto their closing delimiter."""
    return {
        name: dialect.quote_pairs[int(name[1:])][1]
        for name in master.groupindex
        if name.startswith("q") and name[1:].isdigit()
    }


def tokenize(sql: str, dialect: str | Dialect = "postgres") -> list[Token]:
    """Tokenize *sql*, returning tokens ending with an EOF token.

    Raises LexError for unterminated strings/comments and for characters that
    cannot start any SQL token.
    """
    if isinstance(dialect, str):
        dialect = get_dialect(dialect)
    master = _build_master(dialect)
    kind_by_index = _kind_by_index(dialect, master)
    quotes = _quote_lookup(dialect, master)
    nested_comments = dialect.name == "postgres"
    dollar_quotes = dialect.supports_dollar_quote

    tokens: list[Token] = []
    append = tokens.append
    pos = 0
    line = 1
    line_start = 0
    newline_before = False
    space_before = False
    length = len(sql)

    while pos < length:
        match = master.match(sql, pos)
        if match is None:
            col = pos - line_start + 1
            if sql[pos] == "'":
                raise LexError("unterminated string literal", line, col)
            for opener, _ in dialect.quote_pairs:
                if sql[pos] == opener:
                    raise LexError("unterminated quoted identifier", line, col)
            if sql[pos] == "$" and dollar_quotes:
                raise LexError("unterminated dollar-quoted string", line, col)
            if sql[pos] in ("\u201c", "\u201d"):
                raise LexError(
                    "unexpected smart quote; quote identifiers with \"double quotes\" "
                    "and strings with 'single quotes'",
                    line,
                    col,
                )
            raise LexError(f"unexpected character {sql[pos]!r}", line, col)

        start = match.start()
        end = match.end()
        kind = kind_by_index.get(match.lastindex or 0, "")
        text = match.group()

        if kind == "ws":
            space_before = True
            if "\n" in text:
                newline_before = True
                line += text.count("\n")
                line_start = start + text.rfind("\n") + 1
            pos = end
            continue

        col = start - line_start + 1
        if kind == "badcomment":
            raise LexError("unterminated block comment", line, col)
        if kind == "word":
            append(Token(WORD, text, line, col, False, newline_before, space_before, text.upper()))
        elif kind in ("number", "op", "punct", "placeholder"):
            append(Token(_KIND_NAMES[kind], text, line, col, False, newline_before, space_before))
        elif kind == "lcomment":
            append(Token(COMMENT, text, line, col, False, newline_before, space_before))
        elif kind == "bcomment":
            if nested_comments and text.count("/*") > 1:
                end = _scan_nested_comment(sql, start, line, col)
                text = sql[start:end]
            append(Token(COMMENT, text, line, col, False, newline_before, space_before))
        elif kind == "string":
            append(
                Token(STRING, match.group("string"), line, col, False, newline_before, space_before)
            )
        elif kind in ("dopen", "dbody", "dbody2"):
            body = match.group("dbody")
            if body is None:
                body = match.group("dbody2")
            append(Token(STRING, body, line, col, False, newline_before, space_before))
        else:
            closer = quotes.get(kind)
            if closer is None:
                raise LexError(f"unexpected character {text[:1]!r}", line, col)
            append(
                Token(
                    QUOTED,
                    _unquote(text[1:-1], closer),
                    line,
                    col,
                    True,
                    newline_before,
                    space_before,
                    "",
                    text,
                )
            )

        last = tokens[-1]
        if isinstance(last, Token) and last.start < 0:
            last.start = start
            last.end = end
        if "\n" in text:
            line += text.count("\n")
            line_start = start + text.rfind("\n") + 1
        newline_before = False
        space_before = False
        pos = end

    tokens.append(Token(EOF, "", line, pos - line_start + 1))
    return tokens


def _unquote(value: str, closer: str) -> str:
    """Undo doubled closing quotes inside a quoted identifier."""
    return value.replace(closer * 2, closer)


def _scan_nested_comment(sql: str, start: int, line: int, col: int) -> int:
    """PostgreSQL block comments nest; find the matching end manually."""
    depth = 0
    i = start
    length = len(sql)
    while i < length:
        if sql.startswith("/*", i):
            depth += 1
            i += 2
        elif sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    raise LexError("unterminated block comment", line, col)


def strip_comments(tokens: list[Token]) -> list[Token]:
    """Return *tokens* without comments (EOF is preserved)."""
    return [t for t in tokens if t.kind != COMMENT]