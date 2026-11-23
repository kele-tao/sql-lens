"""A recursive-descent parser for a practical SQL subset.

Supported statements: SELECT (with CTEs, joins, subqueries, set operations),
INSERT, UPDATE, DELETE. Anything else (DDL, transactions, PRAGMA) is preserved
as a RawStatement so DDL rules can still inspect it token by token.

The parser is deliberately strict about the shape of a SELECT list and loose
about expressions: an unknown construct degrades to a Raw expression node rather
than failing the file, because a linter that refuses to look at a query is worse
than one that analyses 95% of it.
"""

from __future__ import annotations

from .dialects import Dialect, get_dialect
from .lexer import (
    COMMENT,
    EOF,
    NUMBER,
    OP,
    PLACEHOLDER,
    PUNCT,
    QUOTED,
    STRING,
    WORD,
    Token,
    tokenize,
)
from .model import (
    CTE,
    BinOp,
    CaseExpr,
    Cast,
    Column,
    ColumnItem,
    Delete,
    Expr,
    Func,
    Insert,
    Join,
    Literal,
    Node,
    OrderItem,
    RawStatement,
    Select,
    Star,
    Subquery,
    SubqueryRef,
    TableRef,
    UnOp,
    Update,
)

SET_OP_WORDS = {"UNION", "INTERSECT", "EXCEPT", "MINUS"}
STATEMENT_STARTERS = {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE"}
JOIN_WORDS = {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL"}
COMPARISON_OPS = {"=", "<>", "!=", "<", ">", "<=", ">=", "<=>", "!~", "~*", "->", "->>", "#>", "#>>"}
CLAUSE_WORDS = {
    "FROM",
    "SET",
    "VALUES",
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
    "INTO",
}


class ParseError(ValueError):
    """Raised when the token stream does not match the grammar."""

    def __init__(self, message: str, line: int, col: int, token: str = "") -> None:
        where = f"line {line}, column {col}"
        found = f" near {token!r}" if token else ""
        super().__init__(f"{where}: {message}{found}")
        self.line = line
        self.col = col
        self.detail = message
        self.token = token


class Parser:
    """Cursor-based recursive-descent parser over a token list."""

    def __init__(self, tokens: list[Token], dialect: Dialect) -> None:
        self.tokens = tokens
        self.pos = 0
        self.dialect = dialect
        self.depth = 0
        self.max_depth = 0

    # -- helpers ---------------------------------------------------------- #

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 1) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.kind != EOF:
            self.pos += 1
        return tok

    def at_word(self, *words: str) -> bool:
        return self.current.is_word(*words)

    def at_punct(self, *values: str) -> bool:
        return self.current.kind == PUNCT and self.current.value in values

    def at_op(self, *values: str) -> bool:
        return self.current.kind == OP and self.current.value in values

    def eat_word(self, word: str) -> Token | None:
        if self.at_word(word):
            return self.advance()
        return None

    def expect_word(self, word: str) -> Token:
        tok = self.eat_word(word)
        if tok is None:
            raise ParseError(
                f"expected {word}",
                self.current.line,
                self.current.col,
                self.current.value,
            )
        return tok

    def expect_punct(self, value: str) -> Token:
        if not self.at_punct(value):
            raise ParseError(
                f"expected {value!r}",
                self.current.line,
                self.current.col,
                self.current.value,
            )
        return self.advance()

    def skip_comments(self) -> None:
        while self.current.kind == COMMENT:
            self.advance()

    def error(self, message: str) -> ParseError:
        return ParseError(message, self.current.line, self.current.col, self.current.value)

    # -- entry points ----------------------------------------------------- #

    def parse_statements(self) -> list[Node]:
        out: list[Node] = []
        self.skip_comments()
        while self.current.kind != EOF:
            if self.at_punct(";"):
                self.advance()
                self.skip_comments()
                continue
            start = self.pos
            stmt = self.parse_statement()
            stmt.tokens = [t for t in self.tokens[start : self.pos] if t.kind != COMMENT]
            out.append(stmt)
            self.skip_comments()
            if self.at_punct(";"):
                self.advance()
                self.skip_comments()
            elif self.current.kind != EOF:
                raise self.error("expected ';' between statements")
        return out

    def parse_statement(self) -> Node:
        self.skip_comments()
        tok = self.current
        if tok.kind != WORD:
            raise self.error("expected a SQL statement")
        word = tok.upper
        if word == "WITH":
            return self.parse_with()
        if word == "SELECT":
            return self.parse_select()
        if word == "INSERT":
            return self.parse_insert()
        if word == "UPDATE":
            return self.parse_update()
        if word == "DELETE":
            return self.parse_delete()
        return self.parse_raw(word)

    def parse_raw(self, keyword: str) -> RawStatement:
        start = self.current
        depth = 0
        while self.current.kind != EOF:
            if self.at_punct(";") and depth == 0:
                break
            if self.at_punct("("):
                depth += 1
            elif self.at_punct(")"):
                depth = max(0, depth - 1)
            self.advance()
        return RawStatement(line=start.line, col=start.col, keyword=keyword)

    # -- WITH ------------------------------------------------------------- #

    def parse_with(self) -> Select:
        with_tok = self.advance()
        recursive = bool(self.eat_word("RECURSIVE"))
        ctes: list[CTE] = []
        while True:
            self.skip_comments()
            name_tok = self.current
            if name_tok.kind not in {WORD, QUOTED}:
                raise self.error("expected a CTE name after WITH")
            self.advance()
            columns: list[str] = []
            if self.at_punct("("):
                self.advance()
                while not self.at_punct(")"):
                    col_tok = self.advance()
                    if col_tok.kind not in {WORD, QUOTED}:
                        raise self.error("expected a CTE column name")
                    columns.append(col_tok.value)
                    if self.at_punct(","):
                        self.advance()
                    elif not self.at_punct(")"):
                        raise self.error("expected ',' or ')' in CTE column list")
                self.advance()
            self.expect_word("AS")
            self.eat_word("NOT")
            self.eat_word("MATERIALIZED")
            self.expect_punct("(")
            inner = self.parse_statement()
            if not isinstance(inner, Select):
                raise ParseError(
                    "a CTE must contain a SELECT",
                    inner.line,
                    inner.col,
                    "",
                )
            self.expect_punct(")")
            ctes.append(
                CTE(
                    line=name_tok.line,
                    col=name_tok.col,
                    name=name_tok.value,
                    columns=columns,
                    query=inner,
                    recursive=recursive,
                )
            )
            if self.at_punct(","):
                self.advance()
                continue
            break
        main = self.parse_select()
        main.ctes = ctes + main.ctes
        main.line = with_tok.line
        main.col = with_tok.col
        return main

    # -- SELECT ----------------------------------------------------------- #

    def parse_select(self) -> Select:
        select_tok = self.expect_word("SELECT")
        node = Select(line=select_tok.line, col=select_tok.col)
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)

        if self.at_word("DISTINCT"):
            self.advance()
            node.distinct = True
            if self.at_word("ON"):
                self.advance()
                if self.at_punct("("):
                    self._skip_balanced("(", ")")
        elif self.at_word("ALL"):
            self.advance()

        node.columns = self.parse_select_list()
        self.skip_comments()
        node.from_items, node.joins = self.parse_from()
        self.skip_comments()

        if self.at_word("WHERE"):
            self.advance()
            node.where = self.parse_expression()
            self.skip_comments()

        if self.at_word("GROUP"):
            self.advance()
            self.expect_word("BY")
            node.group_by = self.parse_expression_list()

        if self.at_word("HAVING"):
            self.advance()
            node.having = self.parse_expression()

        self.skip_comments()

        if self.at_word("WINDOW"):
            while self.current.kind != EOF and not self.at_word(*CLAUSE_WORDS):
                self.advance()

        while self.at_word(*SET_OP_WORDS):
            op = self.advance().upper
            if self.at_word("ALL", "DISTINCT"):
                self.advance()
            right = self.parse_select()
            node.set_ops.append((op, right))

        self.skip_comments()
        if self.at_word("ORDER"):
            self.advance()
            self.expect_word("BY")
            node.order_by = self.parse_order_list()
            self.skip_comments()

        self._parse_limit_clause(node)
        self._parse_fetch_clause(node)

        if self.at_word("FOR"):
            # FOR UPDATE / FOR SHARE locks are not modelled; skip them.
            while self.current.kind != EOF and not self.at_punct(";"):
                if self.at_word(*SET_OP_WORDS):
                    break
                self.advance()

        self.depth -= 1
        return node

    def _skip_balanced(self, opener: str, closer: str) -> None:
        self.expect_punct(opener)
        depth = 1
        while depth and self.current.kind != EOF:
            if self.at_punct(opener):
                depth += 1
            elif self.at_punct(closer):
                depth -= 1
            self.advance()

    def _parse_limit_clause(self, node: Select) -> None:
        if self.at_word("LIMIT"):
            self.advance()
            if self.at_word("ALL"):
                self.advance()
            else:
                node.limit = self.parse_expression()
            if self.at_punct(",") and self.dialect.name == "mysql":
                # MySQL's LIMIT offset, count
                self.advance()
                node.offset = node.limit
                node.limit = self.parse_expression()
            elif self.at_word("OFFSET"):
                self.advance()
                node.offset = self.parse_expression()

    def _parse_fetch_clause(self, node: Select) -> None:
        if self.at_word("OFFSET") and node.offset is None:
            self.advance()
            node.offset = self.parse_expression()
            if self.at_word("ROW", "ROWS"):
                self.advance()
        if self.at_word("FETCH"):
            self.advance()
            if self.at_word("FIRST", "NEXT"):
                self.advance()
            if self.current.kind == NUMBER:
                node.limit = self.parse_expression()
            while self.at_word("ROW", "ROWS", "ONLY", "WITH", "TIES"):
                self.advance()

    def parse_select_list(self) -> list[ColumnItem]:
        items: list[ColumnItem] = []
        while True:
            self.skip_comments()
            start = self.current
            expr = self.parse_expression()
            alias: str | None = None
            alias_quoted = False
            if self.at_word("AS"):
                self.advance()
                alias_tok = self.current
                if alias_tok.kind not in {WORD, QUOTED, STRING}:
                    raise self.error("expected an alias after AS")
                self.advance()
                alias = alias_tok.value
                alias_quoted = alias_tok.kind == QUOTED
            elif self.current.kind == QUOTED:
                alias = self.advance().value
                alias_quoted = True
            elif (
                self.current.kind == WORD
                and not self.at_word(*CLAUSE_WORDS)
                and not self.at_word(*JOIN_WORDS)
                and not self.at_word("ON", "USING", "AND", "OR", "NOT", "IS", "IN", "LIKE")
            ):
                alias = self.advance().value
            items.append(
                ColumnItem(
                    line=start.line,
                    col=start.col,
                    expr=expr,
                    alias=alias,
                    alias_quoted=alias_quoted,
                )
            )
            if self.at_punct(","):
                self.advance()
                self.skip_comments()
                if self.at_punct(")") or self.current.kind == EOF:
                    raise ParseError(
                        "trailing comma in select list",
                        self.tokens[self.pos - 1].line,
                        self.tokens[self.pos - 1].col,
                        ",",
                    )
                if self.current.is_word(*CLAUSE_WORDS):
                    # 'group' or 'order' can be a column name: only treat the
                    # comma as trailing when what follows cannot be a column.
                    saved = self.pos
                    try:
                        self.parse_expression()
                        usable = (
                            self.at_punct(",", ")", ";")
                            or self.current.kind == EOF
                            or self.at_word(*CLAUSE_WORDS)
                        )
                    except ParseError:
                        usable = False
                    self.pos = saved
                    if not usable:
                        raise ParseError(
                            "trailing comma in select list",
                            self.tokens[self.pos - 1].line,
                            self.tokens[self.pos - 1].col,
                            ",",
                        )
                continue
            break
        return items

    def parse_from(self) -> tuple[list[TableRef | SubqueryRef], list[Join]]:
        items: list[TableRef | SubqueryRef] = []
        joins: list[Join] = []
        self.skip_comments()
        if not self.at_word("FROM"):
            return items, joins
        self.advance()
        while True:
            self.skip_comments()
            items.append(self.parse_table_ref())
            while self.at_word(*JOIN_WORDS):
                joins.append(self.parse_join())
            if self.at_punct(","):
                self.advance()
                continue
            break
        return items, joins

    def parse_table_ref(self, allow_call: bool = True) -> TableRef | SubqueryRef:
        self.skip_comments()
        if self.at_punct("("):
            start = self.current
            self.advance()
            if self.at_word("SELECT", "WITH"):
                inner = self.parse_statement()
                if not isinstance(inner, Select):
                    raise self.error("expected a SELECT inside a derived table")
                self.expect_punct(")")
                alias = self.parse_alias()
                return SubqueryRef(line=start.line, col=start.col, query=inner, alias=alias)
            # parenthesised join group: consume the group and treat as opaque
            depth = 1
            while depth and self.current.kind != EOF:
                if self.at_punct("("):
                    depth += 1
                elif self.at_punct(")"):
                    depth -= 1
                self.advance()
            alias = self.parse_alias()
            return TableRef(line=start.line, col=start.col, name="(join group)", alias=alias)

        tok = self.current
        if tok.kind not in {WORD, QUOTED}:
            raise self.error("expected a table name")
        self.advance()
        parts = [tok.value]
        quoted = tok.kind == QUOTED
        while self.at_punct("."):
            self.advance()
            nxt = self.current
            if nxt.kind not in {WORD, QUOTED}:
                raise self.error("expected an identifier after '.'")
            self.advance()
            parts.append(nxt.value)
            quoted = quoted or nxt.kind == QUOTED
        schema = ".".join(parts[:-1]) if len(parts) > 1 else None
        name = parts[-1]
        # table-valued functions such as generate_series(1, 10): skip args
        if allow_call and self.at_punct("("):
            self._skip_balanced("(", ")")
        alias = self.parse_alias()
        return TableRef(
            line=tok.line,
            col=tok.col,
            name=name,
            schema=schema,
            alias=alias,
            quoted=quoted,
        )

    def parse_alias(self) -> str | None:
        self.skip_comments()
        if self.at_word("AS"):
            self.advance()
            tok = self.current
            if tok.kind not in {WORD, QUOTED}:
                raise self.error("expected an alias after AS")
            self.advance()
            return tok.value
        if self.current.kind == QUOTED:
            return self.advance().value
        if (
            self.current.kind == WORD
            and not self.at_word(
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
                "ON",
                "USING",
                "JOIN",
                "INNER",
                "LEFT",
                "RIGHT",
                "FULL",
                "CROSS",
                "NATURAL",
                "SET",
                "RETURNING",
                "AND",
                "OR",
                "FOR",
                "WINDOW",
            )
        ):
            return self.advance().value
        return None

    def parse_join(self) -> Join:
        start = self.current
        kind = "INNER"
        natural = False
        if self.at_word("NATURAL"):
            natural = True
            self.advance()
        if self.at_word("CROSS"):
            kind = "CROSS"
            self.advance()
        elif self.at_word("INNER"):
            self.advance()
        elif self.at_word("LEFT"):
            self.advance()
            self.eat_word("OUTER")
            kind = "LEFT"
        elif self.at_word("RIGHT"):
            self.advance()
            self.eat_word("OUTER")
            kind = "RIGHT"
        elif self.at_word("FULL"):
            self.advance()
            self.eat_word("OUTER")
            kind = "FULL"
        self.expect_word("JOIN")
        target = self.parse_table_ref()
        join = Join(line=start.line, col=start.col, kind=kind, target=target, natural=natural)
        if self.at_word("ON"):
            self.advance()
            join.on = self.parse_expression()
        elif self.at_word("USING"):
            self.advance()
            self.expect_punct("(")
            while not self.at_punct(")"):
                tok = self.advance()
                if tok.kind not in {WORD, QUOTED}:
                    raise self.error("expected a column name in USING")
                join.using.append(tok.value)
                if self.at_punct(","):
                    self.advance()
            self.advance()
        return join

    def parse_order_list(self) -> list[OrderItem]:
        items: list[OrderItem] = []
        while True:
            start = self.current
            if self.current.kind in {WORD, QUOTED} and self.peek().kind in {WORD} and self.peek().is_word(
                "ASC", "DESC"
            ):
                pass
            expr = self.parse_expression()
            desc = False
            if self.at_word("DESC"):
                self.advance()
                desc = True
            elif self.at_word("ASC"):
                self.advance()
            nulls = ""
            if self.at_word("NULLS"):
                self.advance()
                if self.at_word("FIRST", "LAST"):
                    nulls = self.advance().upper
            items.append(
                OrderItem(line=start.line, col=start.col, expr=expr, descending=desc, nulls=nulls)
            )
            if self.at_punct(","):
                self.advance()
                continue
            break
        return items

    # -- DML -------------------------------------------------------------- #

    def parse_insert(self) -> Insert:
        tok = self.expect_word("INSERT")
        self.expect_word("INTO")
        table = self.parse_table_ref(allow_call=False)
        if not isinstance(table, TableRef):
            raise self.error("INSERT target must be a table")
        node = Insert(line=tok.line, col=tok.col, table=table)
        if self.at_punct("("):
            self.advance()
            while not self.at_punct(")"):
                col = self.advance()
                if col.kind not in {WORD, QUOTED}:
                    raise self.error("expected a column name in INSERT")
                node.columns.append(col.value)
                if self.at_punct(","):
                    self.advance()
                elif not self.at_punct(")"):
                    raise self.error("expected ',' or ')' in INSERT column list")
            self.advance()
        if self.at_word("VALUES", "VALUE"):
            self.advance()
            while True:
                self.expect_punct("(")
                row: list[Expr] = []
                if not self.at_punct(")"):
                    while True:
                        row.append(self.parse_expression())
                        if self.at_punct(","):
                            self.advance()
                            continue
                        break
                self.expect_punct(")")
                node.rows.append(row)
                if self.at_punct(","):
                    self.advance()
                    continue
                break
        elif self.at_word("SELECT", "WITH"):
            inner = self.parse_statement()
            if not isinstance(inner, Select):
                raise self.error("INSERT ... SELECT requires a SELECT")
            node.select = inner
        elif self.at_word("DEFAULT"):
            self.advance()
            self.expect_word("VALUES")
        else:
            raise self.error("expected VALUES or SELECT after INSERT")
        if self.at_word("RETURNING"):
            self.advance()
            node.returning = self.parse_select_list()
        return node

    def parse_update(self) -> Update:
        tok = self.expect_word("UPDATE")
        self.eat_word("ONLY")
        table = self.parse_table_ref(allow_call=False)
        if not isinstance(table, TableRef):
            raise self.error("UPDATE target must be a table")
        node = Update(line=tok.line, col=tok.col, table=table)
        self.expect_word("SET")
        while True:
            name_tok = self.current
            if name_tok.kind not in {WORD, QUOTED}:
                raise self.error("expected a column name in SET")
            self.advance()
            name = name_tok.value
            while self.at_punct("."):
                self.advance()
                name = self.advance().value
            if not self.at_op("="):
                raise self.error("expected '=' in SET")
            self.advance()
            node.assignments.append((name, self.parse_expression()))
            if self.at_punct(","):
                self.advance()
                continue
            break
        if self.at_word("FROM"):
            self.parse_from()
        if self.at_word("WHERE"):
            self.advance()
            node.where = self.parse_expression()
        if self.at_word("RETURNING"):
            while self.current.kind != EOF and not self.at_punct(";"):
                self.advance()
        if self.at_word("LIMIT"):
            self.advance()
            node.limit = self.parse_expression()
        return node

    def parse_delete(self) -> Delete:
        tok = self.expect_word("DELETE")
        self.expect_word("FROM")
        self.eat_word("ONLY")
        table = self.parse_table_ref(allow_call=False)
        if not isinstance(table, TableRef):
            raise self.error("DELETE target must be a table")
        node = Delete(line=tok.line, col=tok.col, table=table)
        if self.at_word("USING"):
            self.advance()
            self.parse_from()
        if self.at_word("WHERE"):
            self.advance()
            node.where = self.parse_expression()
        if self.at_word("RETURNING"):
            while self.current.kind != EOF and not self.at_punct(";"):
                self.advance()
        if self.at_word("LIMIT"):
            self.advance()
            node.limit = self.parse_expression()
        return node

    # -- expressions ------------------------------------------------------ #

    def parse_expression_list(self) -> list[Expr]:
        out = [self.parse_expression()]
        while self.at_punct(","):
            self.advance()
            out.append(self.parse_expression())
        return out

    def parse_expression(self) -> Expr:
        return self.parse_or()

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.at_word("OR"):
            tok = self.advance()
            right = self.parse_and()
            left = BinOp(line=tok.line, col=tok.col, op="OR", left=left, right=right)
        return left

    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.at_word("AND"):
            tok = self.advance()
            right = self.parse_not()
            left = BinOp(line=tok.line, col=tok.col, op="AND", left=left, right=right)
        return left

    def parse_not(self) -> Expr:
        if self.at_word("NOT"):
            tok = self.advance()
            return UnOp(line=tok.line, col=tok.col, op="NOT", operand=self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Expr:
        left = self.parse_additive()
        while True:
            tok = self.current
            if tok.kind == OP and tok.value in COMPARISON_OPS:
                self.advance()
                right = self.parse_additive()
                left = BinOp(line=tok.line, col=tok.col, op=tok.value, left=left, right=right)
                continue
            negated = False
            if self.at_word("NOT") and self.peek().is_word("IN", "LIKE", "ILIKE", "BETWEEN"):
                negated = True
                self.advance()
            if self.at_word("IS"):
                self.advance()
                self.eat_word("NOT")
                if self.at_word("NULL", "TRUE", "FALSE", "DISTINCT", "UNKNOWN"):
                    right = self.parse_primary()
                    left = BinOp(line=tok.line, col=tok.col, op="IS", left=left, right=right)
                else:
                    self.advance()
                continue
            if self.at_word("IN"):
                self.advance()
                self.expect_punct("(")
                values: list[Expr] = []
                if self.at_word("SELECT", "WITH"):
                    inner = self.parse_statement()
                    self.expect_punct(")")
                    sub = Subquery(
                        line=tok.line,
                        col=tok.col,
                        query=inner if isinstance(inner, Select) else None,
                    )
                    values = [sub]
                else:
                    if not self.at_punct(")"):
                        values = self.parse_expression_list()
                    self.expect_punct(")")
                left = BinOp(
                    line=tok.line,
                    col=tok.col,
                    op="NOT IN" if negated else "IN",
                    left=left,
                    right=values[0] if len(values) == 1 else BinOp(op="LIST", left=None, right=None),
                )
                continue
            if self.at_word("LIKE", "ILIKE"):
                op = self.advance().upper
                right = self.parse_additive()
                if self.at_word("ESCAPE"):
                    self.advance()
                    self.parse_additive()
                left = BinOp(
                    line=tok.line, col=tok.col, op=("NOT " if negated else "") + op, left=left, right=right
                )
                continue
            if self.at_word("BETWEEN"):
                self.advance()
                low = self.parse_additive()
                self.expect_word("AND")
                high = self.parse_additive()
                left = BinOp(
                    line=tok.line,
                    col=tok.col,
                    op="BETWEEN",
                    left=left,
                    right=BinOp(op="RANGE", left=low, right=high),
                )
                continue
            if negated:
                raise self.error("expected IN, LIKE or BETWEEN after NOT")
            break
        return left

    def parse_additive(self) -> Expr:
        left = self.parse_multiplicative()
        while self.at_op("+", "-", "||"):
            tok = self.advance()
            right = self.parse_multiplicative()
            left = BinOp(line=tok.line, col=tok.col, op=tok.value, left=left, right=right)
        return left

    def parse_multiplicative(self) -> Expr:
        left = self.parse_unary()
        while self.at_op("/", "%") or self.at_punct("*"):
            tok = self.advance()
            right = self.parse_unary()
            left = BinOp(line=tok.line, col=tok.col, op=tok.value, left=left, right=right)
        return left

    def parse_unary(self) -> Expr:
        if self.at_op("-", "+", "~"):
            tok = self.advance()
            return UnOp(line=tok.line, col=tok.col, op=tok.value, operand=self.parse_unary())
        expr = self.parse_primary()
        while True:
            if self.at_op("::"):
                tok = self.advance()
                type_name = self.parse_type_name()
                expr = Cast(line=tok.line, col=tok.col, expr=expr, type_name=type_name)
                continue
            if self.at_op("->", "->>", "#>", "#>>"):
                tok = self.advance()
                right = self.parse_primary()
                expr = BinOp(line=tok.line, col=tok.col, op=tok.value, left=expr, right=right)
                continue
            break
        return expr

    def parse_type_name(self) -> str:
        parts: list[str] = []
        while self.current.kind == WORD and not self.at_word(*CLAUSE_WORDS):
            word = self.advance().value
            parts.append(word)
            if self.at_punct("("):
                depth = 1
                self.advance()
                while depth and self.current.kind != EOF:
                    if self.at_punct("("):
                        depth += 1
                    elif self.at_punct(")"):
                        depth -= 1
                    elif self.current.kind == NUMBER:
                        parts.append(self.advance().value)
                        continue
                    self.advance()
                parts.append("(...)")
            if self.at_punct("["):
                while not self.at_punct("]") and self.current.kind != EOF:
                    self.advance()
                self.advance()
                parts.append("[]")
            if self.at_punct("."):
                self.advance()
                continue
            break
        return " ".join(parts).upper()

    def parse_primary(self) -> Expr:
        self.skip_comments()
        tok = self.current

        if tok.kind == NUMBER:
            self.advance()
            return Literal(line=tok.line, col=tok.col, kind="number", value=tok.value)
        if tok.kind == STRING:
            self.advance()
            return Literal(line=tok.line, col=tok.col, kind="string", value=tok.value)
        if tok.kind == PLACEHOLDER:
            self.advance()
            return Literal(line=tok.line, col=tok.col, kind="placeholder", value=tok.value)
        if tok.kind == PUNCT and tok.value == "(":
            self.advance()
            if self.at_word("SELECT", "WITH"):
                inner = self.parse_statement()
                self.expect_punct(")")
                return Subquery(
                    line=tok.line,
                    col=tok.col,
                    query=inner if isinstance(inner, Select) else None,
                )
            first = self.parse_expression()
            if self.at_punct(","):
                items = [first]
                while self.at_punct(","):
                    self.advance()
                    items.append(self.parse_expression())
                self.expect_punct(")")
                return BinOp(line=tok.line, col=tok.col, op="TUPLE", left=first)
            self.expect_punct(")")
            return first
        if tok.kind == PUNCT and tok.value == "*":
            self.advance()
            return Star(line=tok.line, col=tok.col)
        if tok.kind == WORD:
            word = tok.upper
            if word == "CASE":
                return self.parse_case()
            if word == "EXISTS":
                self.advance()
                self.expect_punct("(")
                inner = self.parse_statement()
                self.expect_punct(")")
                return Func(
                    line=tok.line,
                    col=tok.col,
                    name="EXISTS",
                    args=[Subquery(query=inner if isinstance(inner, Select) else None)],
                )
            if word == "NULL":
                self.advance()
                return Literal(line=tok.line, col=tok.col, kind="null", value="NULL")
            if word in {"TRUE", "FALSE"}:
                self.advance()
                return Literal(line=tok.line, col=tok.col, kind="bool", value=word)
            if word == "CAST":
                self.advance()
                self.expect_punct("(")
                inner = self.parse_expression()
                self.expect_word("AS")
                type_name = self.parse_type_name()
                self.expect_punct(")")
                return Cast(line=tok.line, col=tok.col, expr=inner, type_name=type_name)
            if word in {"INTERVAL", "DATE", "TIME", "TIMESTAMP"} and self.peek().kind == STRING:
                self.advance()
                lit = self.advance()
                return Cast(
                    line=tok.line,
                    col=tok.col,
                    expr=Literal(kind="string", value=lit.value),
                    type_name=word,
                )
            return self.parse_identifier_expr()

        if tok.kind == QUOTED:
            return self.parse_identifier_expr()
        raise self.error("unexpected token in expression")

    def parse_case(self) -> Expr:
        tok = self.expect_word("CASE")
        branches = 0
        if not self.at_word("WHEN"):
            self.parse_expression()
        while self.at_word("WHEN"):
            self.advance()
            self.parse_expression()
            self.expect_word("THEN")
            self.parse_expression()
            branches += 1
        if self.at_word("ELSE"):
            self.advance()
            self.parse_expression()
        self.expect_word("END")
        return CaseExpr(line=tok.line, col=tok.col, branches=branches)

    def parse_identifier_expr(self) -> Expr:
        tok = self.current
        if tok.kind not in {WORD, QUOTED}:
            raise self.error("expected an identifier")
        self.advance()
        name = tok.value
        quoted = tok.kind == QUOTED

        # function call
        if self.at_punct("(") and not quoted:
            self.advance()
            fn = Func(line=tok.line, col=tok.col, name=name)
            if self.at_punct("*"):
                self.advance()
                fn.star = True
            elif not self.at_punct(")"):
                if self.at_word("DISTINCT"):
                    self.advance()
                    fn.distinct = True
                while True:
                    fn.args.append(self.parse_expression())
                    if self.at_punct(","):
                        self.advance()
                        continue
                    break
            self.expect_punct(")")
            if self.at_word("OVER"):
                self.advance()
                if self.at_punct("("):
                    start = self.pos
                    self._skip_balanced("(", ")")
                    fn.window = " ".join(t.value for t in self.tokens[start : self.pos])
                elif self.current.kind in {WORD, QUOTED}:
                    fn.window = self.advance().value
            if self.at_word("FILTER"):
                self.advance()
                self._skip_balanced("(", ")")
            return fn

        # qualified column: table.column, schema.table.column
        parts = [name]
        quoted_parts = [quoted]
        while self.at_punct("."):
            self.advance()
            if self.at_punct("*"):
                self.advance()
                return Star(line=tok.line, col=tok.col, table=".".join(parts))
            nxt = self.current
            if nxt.kind not in {WORD, QUOTED}:
                raise self.error("expected an identifier after '.'")
            self.advance()
            parts.append(nxt.value)
            quoted_parts.append(nxt.kind == QUOTED)
        if len(parts) == 1:
            return Column(line=tok.line, col=tok.col, name=name, quoted=quoted)
        return Column(
            line=tok.line,
            col=tok.col,
            name=parts[-1],
            table=".".join(parts[:-1]),
            quoted=any(quoted_parts),
        )


def parse(tokens: list[Token], dialect: str | Dialect = "postgres") -> list[Node]:
    """Parse a pre-tokenized SQL string into statements."""
    dia = get_dialect(dialect) if isinstance(dialect, str) else dialect
    return Parser(tokens, dia).parse_statements()


def parse_all(sql: str, dialect: str | Dialect = "postgres") -> list[Node]:
    """Tokenize and parse *sql*, returning every statement."""
    dia = get_dialect(dialect) if isinstance(dialect, str) else dialect
    return Parser(tokenize(sql, dia), dia).parse_statements()


def parse_one(sql: str, dialect: str | Dialect = "postgres") -> Node:
    """Parse *sql* and return its single statement, or raise ParseError."""
    stmts = parse_all(sql, dialect)
    if not stmts:
        raise ParseError("no SQL statement found", 1, 1, "")
    if len(stmts) > 1:
        raise ParseError(
            f"expected exactly one statement, found {len(stmts)}",
            stmts[1].line,
            stmts[1].col,
            "",
        )
    return stmts[0]


def walk(node: object):
    """Yield every AST node reachable from *node* (depth first).

    Dataclasses are traversed generically so new node types are picked up
    automatically by rules that scan expressions.
    """
    from dataclasses import fields, is_dataclass

    stack = [node]
    while stack:
        current = stack.pop()
        if current is None:
            continue
        if isinstance(current, list):
            stack.extend(reversed(current))
            continue
        if isinstance(current, tuple):
            stack.extend(reversed(list(current)))
            continue
        if is_dataclass(current):
            yield current
            for f in fields(current):
                if f.name in {"tokens", "line", "col"}:
                    continue
                stack.append(getattr(current, f.name))


__all__ = ["CLAUSE_WORDS", "ParseError", "Parser", "parse", "parse_all", "parse_one", "walk"]