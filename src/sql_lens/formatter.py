"""A deterministic, opinionated SQL formatter.

The contract is format_sql(format_sql(x)) == format_sql(x): the formatter is a
pure function of the token stream, so formatting already-formatted SQL is a
fixed point. That property is asserted for every bundled example by the tests.

Style (fixed, not configurable - a formatter with 40 options is a bug):

* keywords uppercase, one clause per line,
* two spaces of indentation per nesting level,
* one column per line once a select list has more than one item, with trailing
  commas,
* each JOIN on its own line, its ON condition indented by two spaces,
* subqueries in parentheses broken across lines,
* comments kept on the line where they appear.

Before returning, the formatter re-tokenizes its own output and refuses to
return anything whose token stream differs from its input, so it can never
silently corrupt a query.
"""

from __future__ import annotations

from .dialects import Dialect, get_dialect
from .lexer import (
    COMMENT,
    EOF,
    PUNCT,
    QUOTED,
    STRING,
    WORD,
    Token,
    tokenize,
)
from .parser import parse_all

INDENT = "  "
MAX_WIDTH = 96

CLAUSE_WORDS = frozenset(
    {
        "SELECT",
        "FROM",
        "WHERE",
        "GROUP",
        "HAVING",
        "ORDER",
        "LIMIT",
        "OFFSET",
        "FETCH",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "MINUS",
        "RETURNING",
        "WINDOW",
        "VALUES",
        "SET",
    }
)

JOIN_WORDS = frozenset({"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL"})
SET_OPS = frozenset({"UNION", "INTERSECT", "EXCEPT", "MINUS"})

# Statements the formatter understands well enough to rewrite. Anything else
# (DDL, transactions, PRAGMA) keeps its original text byte for byte.
MODELLED_STATEMENTS = frozenset({"SELECT", "WITH", "INSERT", "UPDATE", "DELETE"})
UPPER_WORDS = frozenset(
    """
    SELECT FROM WHERE GROUP BY HAVING ORDER LIMIT OFFSET FETCH FIRST NEXT ROWS ROW ONLY
    WITH TIES INSERT INTO VALUES UPDATE SET DELETE AS ON USING JOIN INNER LEFT RIGHT FULL
    CROSS NATURAL OUTER AND OR NOT IN IS NULL TRUE FALSE LIKE ILIKE BETWEEN EXISTS CASE
    WHEN THEN ELSE END DISTINCT ALL ANY ASC DESC NULLS UNION INTERSECT EXCEPT MINUS CREATE
    TABLE INDEX VIEW SCHEMA DATABASE DROP ALTER ADD COLUMN RENAME TO TYPE PRIMARY KEY
    FOREIGN REFERENCES DEFAULT CONSTRAINT UNIQUE CHECK CASCADE IF RETURNING RECURSIVE
    MATERIALIZED OVER PARTITION RANGE PRECEDING FOLLOWING CURRENT UNBOUNDED CAST INTERVAL
    DATE TIME TIMESTAMP TOP BEGIN COMMIT ROLLBACK TRANSACTION EXPLAIN ANALYZE VACUUM
    TRUNCATE GRANT REVOKE CONFLICT DO NOTHING
    """.split()
)
NO_SPACE_BEFORE = {",", ";", ")", ".", "::", "]"}
NO_SPACE_AFTER = {".", "::", "["}
PREFIX_OPS = {"-", "+"}


def _token_text(tok: Token, dialect: Dialect) -> str:
    if tok.kind == WORD:
        return tok.upper if tok.upper in UPPER_WORDS else tok.value
    if tok.kind == QUOTED:
        # Prefer the quoting characters the author actually used.
        if tok.raw:
            return tok.raw
        opener, closer = dialect.quote_pairs[0]
        return opener + tok.value.replace(closer, closer * 2) + closer
    if tok.kind == STRING:
        return "'" + tok.value + "'"
    return tok.value


def _needs_space(prev: Token | None, tok: Token) -> bool:
    if prev is None:
        return False
    if tok.value in NO_SPACE_BEFORE or prev.value in NO_SPACE_AFTER:
        return False
    if prev.kind == PUNCT and prev.value == "(":
        return False
    if tok.kind == PUNCT and tok.value == "(":
        return not (prev.kind in (WORD, QUOTED) and prev.value.upper() not in UPPER_WORDS)
    if prev.kind == "OP" and prev.value in PREFIX_OPS:
        return False
    return not (tok.kind == "OP" and tok.value == "::")


def _match_paren(tokens: list[Token], start: int) -> int:
    """Index of the closing paren matching tokens[start], or len(tokens)."""
    depth = 0
    for i in range(start, len(tokens)):
        tok = tokens[i]
        if tok.kind == PUNCT and tok.value == "(":
            depth += 1
        elif tok.kind == PUNCT and tok.value == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(tokens)


def _is_subquery_paren(tokens: list[Token], i: int) -> bool:
    if tokens[i].kind != PUNCT or tokens[i].value != "(":
        return False
    j = i + 1
    while j < len(tokens) and tokens[j].kind == COMMENT:
        j += 1
    return j < len(tokens) and tokens[j].is_word("SELECT", "WITH")


def _split_at_commas(tokens: list[Token]) -> list[list[Token]]:
    """Split a token list on commas at parenthesis depth zero."""
    parts: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for tok in tokens:
        if tok.kind == PUNCT and tok.value == "(":
            depth += 1
        elif tok.kind == PUNCT and tok.value == ")":
            depth = max(0, depth - 1)
        if tok.kind == PUNCT and tok.value == "," and depth == 0:
            parts.append(current)
            current = []
            continue
        current.append(tok)
    if current:
        parts.append(current)
    return parts


def _clause_keyword(tokens: list[Token], i: int) -> tuple[str, int] | None:
    """If tokens[i] starts a clause at depth zero, return (keyword, consumed)."""
    tok = tokens[i]
    if tok.kind != WORD or tok.quoted:
        return None
    upper = tok.upper
    if upper in {"GROUP", "ORDER"}:
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt is not None and nxt.is_word("BY"):
            return f"{upper} BY", 2
        return None
    if upper in SET_OPS:
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt is not None and nxt.is_word("ALL", "DISTINCT"):
            return f"{upper} {nxt.upper}", 2
        return upper, 1
    if upper in CLAUSE_WORDS:
        return upper, 1
    return None


def _split_clauses(tokens: list[Token]) -> list[tuple[str, list[Token]]]:
    """Split a statement into (clause keyword, tokens) at depth zero."""
    parts: list[tuple[str, list[Token]]] = []
    current: list[Token] = []
    current_key = ""
    depth = 0
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        boundary = None
        if (
            depth == 0
            and i > 0
            and tok.kind == WORD
            and not tok.quoted
            and not (tok.upper == "FROM" and tokens[0].is_word("DELETE"))
        ):
            boundary = _clause_keyword(tokens, i)
        if boundary is not None:
            if current:
                parts.append((current_key, current))
            keyword, consumed = boundary
            current_key = keyword
            current = []
            i += consumed
            continue
        if tok.kind == PUNCT and tok.value == "(":
            depth += 1
        elif tok.kind == PUNCT and tok.value == ")":
            depth = max(0, depth - 1)
        current.append(tok)
        i += 1
    if current:
        parts.append((current_key, current))
    return parts


def _split_boolean(tokens: list[Token]) -> list[list[Token]]:
    """Split a predicate at depth-zero AND/OR, keeping the connector."""
    parts: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for tok in tokens:
        if tok.kind == PUNCT and tok.value == "(":
            depth += 1
        elif tok.kind == PUNCT and tok.value == ")":
            depth = max(0, depth - 1)
        if depth == 0 and tok.kind == WORD and tok.is_word("AND", "OR") and current:
            parts.append(current)
            current = [tok]
            continue
        current.append(tok)
    if current:
        parts.append(current)
    return parts


def _line(indent: int, text: str) -> str:
    return INDENT * indent + text


class Formatter:
    """Renders one statement (already split on ';') into a list of lines."""

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect

    # -- inline rendering -------------------------------------------------- #

    def inline(self, tokens: list[Token], indent: int) -> list[str]:
        """Render tokens inline, breaking subqueries onto their own lines."""
        lines: list[str] = []
        cur = INDENT * indent
        prev: Token | None = None
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.kind == COMMENT:
                text = tok.value.strip()
                cur = (cur + " " + text) if cur else _line(indent, text)
                lines.append(cur)
                cur = ""
                prev = None
                i += 1
                continue
            if _is_subquery_paren(tokens, i):
                end = _match_paren(tokens, i)
                if prev is not None and _needs_space(prev, tok):
                    cur += " "
                cur += "("
                lines.append(cur)
                lines.extend(self.statement(tokens[i + 1 : end], indent + 1))
                cur = _line(indent, ")")
                prev = tokens[end] if end < len(tokens) else None
                i = end + 1
                continue
            text = _token_text(tok, self.dialect)
            if _needs_space(prev, tok) and cur and not cur.endswith(" "):
                cur += " "
            cur += text
            if tok.kind == PUNCT and tok.value == "(":
                cur = cur  # the next token decides its own spacing
            prev = tok
            i += 1
        if cur:
            lines.append(cur)
        return lines

    # -- clause rendering -------------------------------------------------- #

    def statement(self, tokens: list[Token], indent: int) -> list[str]:
        tokens = [t for t in tokens if t.kind != EOF]
        if not tokens:
            return []
        leading: list[str] = []
        while tokens and tokens[0].kind == COMMENT:
            leading.extend(_line(indent, text.strip()) for text in tokens[0].value.splitlines())
            tokens = tokens[1:]
        if not tokens:
            return leading
        body = self._statement(tokens, indent)
        return leading + body if leading else body

    def _statement(self, tokens: list[Token], indent: int) -> list[str]:
        first = tokens[0]
        if first.is_word("WITH"):
            return self.with_statement(tokens, indent)
        parts = _split_clauses(tokens)
        lines: list[str] = []
        for index, (keyword, payload) in enumerate(parts):
            lines.extend(self.clause(keyword, payload, indent, index))
        return lines

    def clause(self, keyword: str, tokens: list[Token], indent: int, index: int) -> list[str]:
        upper = keyword.upper()
        if not tokens:
            return []
        if index == 0:
            return self.head_clause(tokens, indent)
        if upper in SET_OPS:
            body = self.statement(tokens, indent)
            if not body:
                return [_line(indent, upper)]
            return [_line(indent, upper), "", *body]
        if upper == "SET":
            items = _split_at_commas(tokens)
            rendered_items = [self.inline(item, indent + 1) for item in items]
            if len(items) == 1:
                return [_line(indent, "SET " + " ".join(rendered_items[0]).strip())]
            lines = [_line(indent, "SET")]
            for pos, rendered in enumerate(rendered_items):
                lines.extend(rendered)
                if pos < len(rendered_items) - 1:
                    lines[-1] += ","
            return lines
        if upper == "VALUES":
            rows = _split_at_commas(tokens)
            lines = [_line(indent, "VALUES")]
            for pos, row in enumerate(rows):
                rendered = self.inline(row, indent + 1)
                if rendered:
                    lines.append(_line(indent + 1, rendered[0].strip()))
                    lines.extend(rendered[1:])
                if pos < len(rows) - 1 and rendered:
                    lines[-1] += ","
            return lines
        if upper == "FROM":
            return self.from_clause(tokens, indent)
        if upper == "SELECT":
            return self.head_clause(tokens, indent)
        segments = _split_boolean(tokens) if upper in {"WHERE", "HAVING"} else [tokens]
        rendered: list[str] = []
        for pos, segment in enumerate(segments):
            body = self.inline(segment, indent + (1 if pos else 0))
            if not body:
                continue
            prefix = upper + " " if pos == 0 else ""
            lead = indent if pos == 0 else indent + 1
            rendered.append(_line(lead, prefix + body[0].strip()))
            rendered.extend(body[1:])
        return rendered or [_line(indent, upper)]

    def head_clause(self, tokens: list[Token], indent: int) -> list[str]:
        first = tokens[0]
        if first.is_word("SELECT"):
            return self.select_list(tokens, indent)
        rendered = self.inline(tokens, indent)
        return [_line(indent, rendered[0].strip()), *rendered[1:]]

    def select_list(self, tokens: list[Token], indent: int) -> list[str]:
        prefix = "SELECT"
        rest = tokens[1:]
        while rest and rest[0].kind == WORD and rest[0].is_word("DISTINCT", "ALL"):
            prefix += " " + rest[0].upper
            rest = rest[1:]
            if rest and rest[0].is_word("ON") and prefix.endswith("DISTINCT"):
                end = _match_paren(rest, 1) if len(rest) > 1 else 1
                joined = "".join(_token_text(t, self.dialect) for t in rest[: end + 1])
                prefix += " ON " + joined
                rest = rest[end + 1 :]
        items = _split_at_commas(rest)
        if len(items) == 1:
            body = self.inline(items[0], indent + 1)
            if len(body) == 1:
                candidate = prefix + " " + body[0].strip()
                if len(candidate) <= MAX_WIDTH:
                    return [_line(indent, candidate)]
                return [_line(indent, prefix), _line(indent + 1, body[0].strip())]
        lines = [_line(indent, prefix)]
        for pos, item in enumerate(items):
            rendered = self.inline(item, indent + 1)
            if rendered:
                lines.append(_line(indent + 1, rendered[0].strip()))
                lines.extend(rendered[1:])
            if pos < len(items) - 1 and rendered:
                lines[-1] += ","
        return lines

    def from_clause(self, tokens: list[Token], indent: int) -> list[str]:
        segments: list[list[Token]] = []
        current: list[Token] = []
        depth = 0
        for tok in tokens:
            if tok.kind == PUNCT and tok.value == "(":
                depth += 1
            elif tok.kind == PUNCT and tok.value == ")":
                depth = max(0, depth - 1)
            if (
                depth == 0
                and tok.kind == WORD
                and not tok.quoted
                and (tok.is_word(*JOIN_WORDS) or tok.is_word("ON"))
            ):
                if current:
                    segments.append(current)
                current = []
            current.append(tok)
        if current:
            segments.append(current)

        lines: list[str] = []
        for pos, segment in enumerate(segments):
            if not segment:
                continue
            lead = segment[0]
            rendered = self.inline(segment, indent)
            if pos == 0:
                lines.append(_line(indent, "FROM " + rendered[0].strip()))
                lines.extend(rendered[1:])
                continue
            if lead.is_word("ON"):
                body = self.inline(segment[1:], indent + 1)
                if body:
                    lines.append(_line(indent + 1, "ON " + body[0].strip()))
                    lines.extend(body[1:])
                continue
            lines.append(_line(indent, rendered[0].strip()))
            lines.extend(rendered[1:])
        return lines

    def with_statement(self, tokens: list[Token], indent: int) -> list[str]:
        pos = 1
        header = "WITH"
        if pos < len(tokens) and tokens[pos].is_word("RECURSIVE"):
            header += " RECURSIVE"
            pos += 1
        lines = [_line(indent, header)]
        while pos < len(tokens):
            name_tokens = [tokens[pos]]
            pos += 1
            if pos < len(tokens) and tokens[pos].kind == PUNCT and tokens[pos].value == "(":
                end = _match_paren(tokens, pos)
                name_tokens.extend(tokens[pos : end + 1])
                pos = end + 1
            as_pos = pos
            while as_pos < len(tokens) and not tokens[as_pos].is_word("AS"):
                as_pos += 1
            pos = as_pos + 1
            while pos < len(tokens) and tokens[pos].is_word("NOT", "MATERIALIZED"):
                pos += 1
            if pos >= len(tokens) or tokens[pos].kind != PUNCT or tokens[pos].value != "(":
                lines.extend(self.inline(tokens[1:], indent + 1))
                return lines
            end = _match_paren(tokens, pos)
            header_text = " ".join(_token_text(t, self.dialect) for t in name_tokens)
            lines.append(_line(indent + 1, header_text + " AS ("))
            lines.extend(self.statement(tokens[pos + 1 : end], indent + 2))
            pos = end + 1
            if pos < len(tokens) and tokens[pos].kind == PUNCT and tokens[pos].value == ",":
                lines.append(_line(indent + 1, "),"))
                pos += 1
                continue
            lines.append(_line(indent + 1, ")"))
            break
        if pos < len(tokens):
            lines.extend(self.statement(tokens[pos:], indent))
        return lines


def _split_statements(tokens: list[Token]) -> list[list[Token]]:
    out: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for tok in tokens:
        if tok.kind == PUNCT and tok.value == "(":
            depth += 1
        elif tok.kind == PUNCT and tok.value == ")":
            depth = max(0, depth - 1)
        if tok.kind == PUNCT and tok.value == ";" and depth == 0:
            current.append(tok)
            out.append(current)
            current = []
            continue
        current.append(tok)
    if current:
        out.append(current)
    return out


def _signature(sql: str, dialect: Dialect) -> list[str]:
    """Whitespace- and keyword-case-insensitive view of a query."""
    sig: list[str] = []
    for tok in tokenize(sql, dialect):
        if tok.kind in {EOF, COMMENT}:
            continue
        if tok.kind == WORD and not tok.quoted:
            sig.append("W:" + tok.upper)
        else:
            sig.append(tok.kind + ":" + tok.value)
    return sig


def format_sql(sql: str, dialect: str | Dialect = "postgres", verify: bool = True) -> str:
    """Format *sql* deterministically and idempotently.

    Raises ValueError if formatting would change the token stream: that would
    mean a formatter bug, and quietly corrupting a query is never acceptable.
    """
    dia = get_dialect(dialect) if isinstance(dialect, str) else dialect
    tokens = tokenize(sql, dia)
    if all(t.kind == EOF for t in tokens):
        return ""
    if verify:
        # Refuse to "format" something that is not valid SQL: silently
        # reformatting a broken file hides the real bug.
        parse_all(sql, dia)
    formatter = Formatter(dia)
    rendered: list[str] = []
    for chunk in _split_statements(tokens):
        body = [t for t in chunk if t.kind != EOF]
        if not body:
            continue
        trailing_semicolon = body[-1].kind == PUNCT and body[-1].value == ";"
        first_token = next((t for t in body if t.kind != COMMENT), None)
        modelled = (
            first_token is not None
            and first_token.is_word(*MODELLED_STATEMENTS)
            and body[0].start >= 0
        )
        original = sql[body[0].start : body[-1].end] if body[0].start >= 0 else ""
        if trailing_semicolon:
            body = body[:-1]
        if not modelled and original:
            # Statements the parser does not model (DDL, transactions, PRAGMA)
            # keep their original text: rewriting syntax we do not understand is
            # how formatters corrupt migrations.
            rendered.append(original.strip())
            continue
        lines = formatter.statement(body, 0)
        if not lines:
            continue
        if trailing_semicolon:
            lines[-1] = lines[-1].rstrip() + ";"
        rendered.append("\n".join(lines))
    out = "\n\n".join(rendered)
    out = out + "\n" if out else ""
    if verify and _signature(sql, dia) != _signature(out, dia):
        raise ValueError("formatting would change the query; refusing to rewrite it")
    return out


__all__ = ["Formatter", "format_sql"]