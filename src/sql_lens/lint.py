"""The lint engine: 20 rules with stable codes SL001-SL020.

Every rule is a small function over one parsed file. Rules never talk to each
other and never touch the filesystem; they receive a FileContext and yield
Findings. That keeps them testable one by one and keeps the output stable.

Design notes
------------
* Rules that need a schema (is this column nullable? is it indexed?) are written
  to be schema-free: they flag the *pattern* and explain the risk instead of
  pretending to know your DDL.
* Findings carry a line and column taken from the token that caused them, so
  editors and CI annotations can point at real code.
* The lint runs on the token stream as well as the AST: style rules (mixed case,
  trailing comma) are far easier to see in tokens.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from .dialects import Dialect, get_dialect
from .lexer import EOF, PUNCT, WORD, LexError, Token, tokenize
from .model import (
    BinOp,
    CaseExpr,
    Cast,
    Column,
    Delete,
    Expr,
    Finding,
    Func,
    Insert,
    Literal,
    Node,
    RawStatement,
    Select,
    Severity,
    Star,
    Subquery,
    SubqueryRef,
    TableRef,
    UnOp,
    Update,
)
from .parser import ParseError, parse
from .render import render_html, render_markdown, render_text

# --------------------------------------------------------------------------- #
# Rule catalogue
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Rule:
    """Static metadata for one lint rule."""

    code: str
    name: str
    severity: Severity
    summary: str
    hint: str
    example: str


RULES: tuple[Rule, ...] = (
    Rule(
        "SL001",
        "select-star",
        Severity.WARNING,
        "SELECT * reads every column, including ones added later.",
        "List the columns you use; a new column silently changes the payload.",
        "SELECT * FROM users;",
    ),
    Rule(
        "SL002",
        "missing-where-dml",
        Severity.ERROR,
        "UPDATE/DELETE without WHERE rewrites or empties the whole table.",
        "Add a WHERE clause, or wrap the statement in a transaction and review it.",
        "DELETE FROM sessions;",
    ),
    Rule(
        "SL003",
        "unbounded-update",
        Severity.WARNING,
        "UPDATE/DELETE without LIMIT holds one long transaction over every row.",
        "Add LIMIT (MySQL) or batch the statement by primary-key ranges.",
        "UPDATE accounts SET locked = true;",
    ),
    Rule(
        "SL004",
        "implicit-cross-join",
        Severity.ERROR,
        "JOIN without ON/USING produces a cartesian product.",
        "Add an ON predicate, or write CROSS JOIN if the product is intended.",
        "FROM orders JOIN customers;",
    ),
    Rule(
        "SL005",
        "non-sargable-predicate",
        Severity.WARNING,
        "A function wraps the column, so no index on it can be used.",
        "Rewrite as a range predicate or index the expression.",
        "WHERE lower(email) = 'a@b.c'",
    ),
    Rule(
        "SL006",
        "leading-wildcard-like",
        Severity.WARNING,
        "LIKE with a leading wildcard cannot use a B-tree index.",
        "Prefer prefix search, a trigram index, or a full-text index.",
        "WHERE name LIKE '%son'",
    ),
    Rule(
        "SL007",
        "not-in-nullable",
        Severity.WARNING,
        "NOT IN returns no rows when the list or subquery yields a NULL.",
        "Use NOT EXISTS, which is NULL-safe and usually faster.",
        "WHERE id NOT IN (SELECT user_id FROM bans)",
    ),
    Rule(
        "SL008",
        "or-in-join",
        Severity.WARNING,
        "OR inside a JOIN condition prevents hash/merge join planning.",
        "Split into two queries with UNION ALL, or move the OR into WHERE.",
        "JOIN t ON t.a = x.a OR t.b = x.b",
    ),
    Rule(
        "SL009",
        "order-by-without-limit",
        Severity.WARNING,
        "ORDER BY without LIMIT sorts the entire result set.",
        "Add LIMIT/OFFSET, or accept the sort explicitly with a comment.",
        "SELECT id FROM events ORDER BY created_at DESC;",
    ),
    Rule(
        "SL010",
        "cartesian-risk",
        Severity.ERROR,
        "Comma-joined tables have no predicate connecting them.",
        "Add a join predicate in WHERE or use explicit JOIN ... ON.",
        "FROM orders, customers WHERE orders.total > 100;",
    ),
    Rule(
        "SL011",
        "redundant-distinct",
        Severity.INFO,
        "DISTINCT cannot change the result here, it only costs a sort.",
        "Remove DISTINCT; GROUP BY already produces unique rows.",
        "SELECT DISTINCT count(*) FROM t GROUP BY kind;",
    ),
    Rule(
        "SL012",
        "unused-cte",
        Severity.WARNING,
        "A CTE is defined but never referenced by the query.",
        "Delete the CTE or reference it; unused CTEs still execute in PostgreSQL.",
        "WITH stale AS (SELECT 1) SELECT 2;",
    ),
    Rule(
        "SL013",
        "correlated-subquery",
        Severity.WARNING,
        "A subquery references outer columns, so it runs once per outer row.",
        "Rewrite as a JOIN or a LATERAL join; keep EXISTS only when it short-circuits.",
        "WHERE EXISTS (SELECT 1 FROM t WHERE t.uid = u.id)",
    ),
    Rule(
        "SL014",
        "hardcoded-literals-dml",
        Severity.INFO,
        "Literals baked into DML bypass prepared statements and plan reuse.",
        "Bind values as parameters ($1, ?, %s) from the application.",
        "UPDATE users SET role = 'admin' WHERE id = 42;",
    ),
    Rule(
        "SL015",
        "ddl-without-if-exists",
        Severity.WARNING,
        "CREATE/DROP without IF [NOT] EXISTS fails when rerun.",
        "Use CREATE TABLE IF NOT EXISTS / DROP INDEX IF EXISTS.",
        "DROP TABLE events;",
    ),
    Rule(
        "SL016",
        "unsafe-column-type-change",
        Severity.WARNING,
        "Changing a column type rewrites the table and takes a strong lock.",
        "Add a new column, backfill in batches, then swap and drop.",
        "ALTER TABLE t ALTER COLUMN id TYPE bigint;",
    ),
    Rule(
        "SL017",
        "missing-schema-prefix",
        Severity.INFO,
        "Unqualified table names resolve through search_path and can surprise you.",
        "Qualify tables as schema.table so the query means one thing.",
        "FROM orders JOIN customers ON ...",
    ),
    Rule(
        "SL018",
        "reserved-word-identifier",
        Severity.WARNING,
        "A reserved word is used as a bare identifier and will not parse everywhere.",
        "Quote the identifier or rename the column.",
        "SELECT order, user FROM sales;",
    ),
    Rule(
        "SL019",
        "mixed-case-keywords",
        Severity.INFO,
        "Keywords in one file use more than one casing.",
        "Pick one style, usually uppercase keywords, and let the formatter enforce it.",
        "select id FROM users where active",
    ),
    Rule(
        "SL020",
        "trailing-comma",
        Severity.WARNING,
        "A comma before a clause boundary is a syntax error in most engines.",
        "Remove the trailing comma.",
        "SELECT id, FROM users;",
    ),
)

RULES_BY_CODE: dict[str, Rule] = {r.code: r for r in RULES}

KEYWORDS = frozenset(
    """
    ADD ALL ALTER AND ANY AS ASC BETWEEN BY CASCADE CASE CAST CHECK COLUMN CONSTRAINT
    CREATE CROSS CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP DEFAULT DELETE DESC
    DISTINCT DROP ELSE END EXCEPT EXISTS FALSE FETCH FILTER FIRST FOR FOREIGN FROM FULL
    GROUP HAVING IF ILIKE IN INNER INSERT INTERSECT INTO IS JOIN LAST LEFT LIKE LIMIT
    MATERIALIZED NATURAL NOT NULL NULLS OFFSET ON ONLY OR ORDER OUTER OVER PARTITION
    PRIMARY RECURSIVE REFERENCES RETURNING RIGHT ROWS SELECT SET TABLE THEN TOP TRUE
    UNION UNIQUE UPDATE USING VALUES WHEN WHERE WINDOW WITH
    """.split()
)

CLAUSE_STARTERS = frozenset(
    """
    FROM WHERE GROUP HAVING ORDER LIMIT OFFSET FETCH UNION INTERSECT EXCEPT MINUS
    RETURNING WINDOW INTO SET VALUES ON USING JOIN INNER LEFT RIGHT FULL CROSS
    NATURAL AND OR
    """.split()
)


@dataclass
class FileContext:
    """Everything the rules need about one file."""

    path: str
    sql: str
    tokens: list[Token]
    statements: list[Node]
    dialect: Dialect
    parse_error: str = ""

    _line_cache: list[str] | None = field(default=None, repr=False, compare=False)

    @property
    def lines(self) -> list[str]:
        """Cached split of the source; rules call this once per finding."""
        if self._line_cache is None:
            self._line_cache = self.sql.splitlines()
        return self._line_cache


@dataclass
class LintReport:
    """Findings for one or more files plus the settings that produced them."""

    findings: list[Finding] = field(default_factory=list)
    parse_errors: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    lines_scanned: int = 0
    rules_enabled: list[str] = field(default_factory=list)
    fail_on: Severity = Severity.ERROR

    def count(self, severity: Severity | None = None) -> int:
        if severity is None:
            return len(self.findings)
        return sum(1 for f in self.findings if f.severity == severity)

    @property
    def max_severity(self) -> Severity | None:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: s.rank)

    def failed(self) -> bool:
        """True when the report should make the process exit 1."""
        if self.parse_errors:
            return True
        if self.fail_on == Severity.ERROR:
            return self.count(Severity.ERROR) > 0
        if self.fail_on == Severity.WARNING:
            return any(f.severity.rank >= 2 for f in self.findings)
        if self.fail_on == Severity.INFO:
            return bool(self.findings)
        return False

    def to_dict(self) -> dict:
        by_rule: dict[str, int] = {}
        for f in self.findings:
            by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        return {
            "tool": "sql-lens",
            "version": "1.0.0",
            "files": self.files,
            "lines_scanned": self.lines_scanned,
            "summary": {
                "total": self.count(),
                "errors": self.count(Severity.ERROR),
                "warnings": self.count(Severity.WARNING),
                "info": self.count(Severity.INFO),
                "failed": self.failed(),
            },
            "by_rule": dict(sorted(by_rule.items())),
            "findings": [f.to_dict() for f in self.findings],
            "parse_errors": self.parse_errors,
        }

    def to_text(self, color: bool = True) -> str:
        return render_text(self, color=color)

    def to_html(self) -> str:
        return render_html(self)

    def to_markdown(self) -> str:
        return render_markdown(self)


# --------------------------------------------------------------------------- #
# Traversal helpers
# --------------------------------------------------------------------------- #


def iter_selects(stmt: Node) -> Iterator[Select]:
    """Yield every SELECT in a statement, including CTEs and subqueries."""
    if isinstance(stmt, Select):
        for cte in stmt.ctes:
            if cte.query is not None:
                yield from iter_selects(cte.query)
        yield stmt
        for expr in _select_expressions(stmt):
            for sub in _expr_subqueries(expr):
                if sub.query is not None:
                    yield from iter_selects(sub.query)
        for item in stmt.from_items:
            if isinstance(item, SubqueryRef) and item.query is not None:
                yield from iter_selects(item.query)
        for join in stmt.joins:
            if isinstance(join.target, SubqueryRef) and join.target.query is not None:
                yield from iter_selects(join.target.query)
        for _, right in stmt.set_ops:
            yield from iter_selects(right)
    elif isinstance(stmt, Insert):
        for row in stmt.rows:
            for expr in row:
                for sub in _expr_subqueries(expr):
                    if sub.query is not None:
                        yield from iter_selects(sub.query)
        if stmt.select is not None:
            yield from iter_selects(stmt.select)
    elif isinstance(stmt, (Update, Delete)):
        exprs: list[Expr] = []
        if stmt.where is not None:
            exprs.append(stmt.where)
        if isinstance(stmt, Update):
            exprs.extend(expr for _, expr in stmt.assignments)
        for expr in exprs:
            for sub in _expr_subqueries(expr):
                if sub.query is not None:
                    yield from iter_selects(sub.query)


def _select_expressions(select: Select) -> Iterator[Expr]:
    for item in select.columns:
        if item.expr is not None:
            yield item.expr
    if select.where is not None:
        yield select.where
    yield from select.group_by
    if select.having is not None:
        yield select.having
    for join in select.joins:
        if join.on is not None:
            yield join.on


def iter_expr(expr: Expr | None, skip_subqueries: bool = True) -> Iterator[Expr]:
    """Yield *expr* and its descendants."""
    stack: list[Expr | None] = [expr]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if isinstance(node, Subquery) and skip_subqueries:
            yield node
            continue
        yield node
        if isinstance(node, BinOp):
            stack.extend([node.right, node.left])
        elif isinstance(node, UnOp):
            stack.append(node.operand)
        elif isinstance(node, Func):
            stack.extend(reversed(node.args))
        elif isinstance(node, Cast):
            stack.append(node.expr)
        elif isinstance(node, Subquery):
            stack.append(node.query)
        elif isinstance(node, CaseExpr):
            continue


def expr_columns(expr: Expr | None, skip_subqueries: bool = True) -> Iterator[Column]:
    for node in iter_expr(expr, skip_subqueries=skip_subqueries):
        if isinstance(node, Column):
            yield node


def expr_subqueries(expr: Expr | None) -> Iterator[Subquery]:
    for node in iter_expr(expr, skip_subqueries=False):
        if isinstance(node, Subquery):
            yield node


def _expr_subqueries(expr: Expr | None) -> Iterator[Subquery]:
    return expr_subqueries(expr)


def flatten_and(expr: Expr | None) -> list[Expr]:
    """Split an AND chain into its conjuncts."""
    out: list[Expr] = []
    stack = [expr]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if isinstance(node, BinOp) and node.op == "AND":
            stack.extend([node.right, node.left])
        else:
            out.append(node)
    return out


def _sources(select: Select) -> dict[str, TableRef]:
    """Map alias -> table for every table in the FROM/JOIN chain."""
    out: dict[str, TableRef] = {}
    for item in select.from_items:
        if isinstance(item, TableRef):
            out[item.effective_name] = item
    for join in select.joins:
        if isinstance(join.target, TableRef):
            out[join.target.effective_name] = join.target
    return out


def _reference_edges(expr: Expr | None, sources: Sequence[str]) -> set[tuple[str, str]]:
    """Find equality predicates that connect two sources (a join graph edge)."""
    edges: set[tuple[str, str]] = set()
    wanted = set(sources)
    for node in flatten_and(expr):
        if not isinstance(node, BinOp) or node.op not in {"=", "<=>"}:
            continue
        left, right = node.left, node.right
        if not isinstance(left, Column) or not isinstance(right, Column):
            continue
        if not left.table or not right.table:
            continue
        if left.table in wanted and right.table in wanted and left.table != right.table:
            edges.add(tuple(sorted((left.table, right.table))))
    return edges


def _components(nodes: Sequence[str], edges: Iterable[tuple[str, str]]) -> list[set[str]]:
    """Union-find over the join graph; used by the cartesian-product rule."""
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    groups: dict[str, set[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), set()).add(n)
    return list(groups.values())


def _cte_names(stmt: Node) -> set[str]:
    names: set[str] = set()
    if isinstance(stmt, Select):
        for cte in stmt.ctes:
            names.add(cte.name.lower())
            if cte.query is not None:
                names |= _cte_names(cte.query)
    return names


def _snippet(ctx: FileContext, line: int, max_len: int = 96) -> str:
    lines = ctx.lines
    if not lines or line < 1 or line > len(lines):
        return ""
    text = lines[line - 1].strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "\u2026"
    return text


def finding(
    ctx: FileContext,
    rule: Rule,
    message: str,
    line: int,
    col: int = 1,
    hint: str | None = None,
) -> Finding:
    return Finding(
        rule=rule.code,
        title=rule.name,
        severity=rule.severity,
        message=message,
        line=line,
        col=col,
        path=ctx.path,
        snippet=_snippet(ctx, line),
        hint=rule.hint if hint is None else hint,
    )


def _describe(node: Expr | None) -> str:
    """Short human description of an expression, for rule messages."""
    if isinstance(node, Column):
        return f"{node.table}.{node.name}" if node.table else node.name
    if isinstance(node, Star):
        return f"{node.table}.*" if node.table else "*"
    if isinstance(node, Func):
        return f"{node.name}(...)"
    if isinstance(node, Literal):
        return "NULL" if node.kind == "null" else node.value
    if isinstance(node, BinOp):
        return f"{_describe(node.left)} {node.op} {_describe(node.right)}"
    if isinstance(node, Cast):
        return f"CAST(... AS {node.type_name})"
    if isinstance(node, Subquery):
        return "(subquery)"
    if isinstance(node, UnOp):
        return f"{node.op} {_describe(node.operand)}"
    return "expression"


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def rule_select_star(ctx: FileContext, select: Select) -> Iterator[Finding]:
    for item in select.columns:
        if not item.is_star:
            continue
        expr = item.expr
        label = f"{expr.table}.*" if isinstance(expr, Star) and expr.table else "*"
        message = (
            f"query selects {label}; every column (and every future column) is fetched"
        )
        yield finding(ctx, RULES_BY_CODE["SL001"], message, item.line, item.col)


def _dml_missing_where(ctx: FileContext, stmt: Update | Delete) -> Iterator[Finding]:
    if stmt.where is None:
        verb = "UPDATE" if isinstance(stmt, Update) else "DELETE"
        target = stmt.table.name if stmt.table else "the table"
        yield finding(
            ctx,
            RULES_BY_CODE["SL002"],
            f"{verb} on {target} has no WHERE clause and touches every row",
            stmt.line,
            stmt.col,
        )
    elif stmt.limit is None:
        verb = "UPDATE" if isinstance(stmt, Update) else "DELETE"
        target = stmt.table.name if stmt.table else "the table"
        yield finding(
            ctx,
            RULES_BY_CODE["SL003"],
            f"{verb} on {target} is unbounded: no LIMIT, one transaction for every matched row",
            stmt.line,
            stmt.col,
        )


def rule_cross_join(ctx: FileContext, select: Select) -> Iterator[Finding]:
    for join in select.joins:
        if join.kind == "CROSS" or join.natural or join.using or join.on is not None:
            continue
        target = join.target
        name = target.name if isinstance(target, TableRef) else "subquery"
        yield finding(
            ctx,
            RULES_BY_CODE["SL004"],
            f"{join.kind} JOIN {name} has no ON or USING clause, so it multiplies rows",
            join.line,
            join.col,
        )


_NON_SARGABLE_FUNCS = frozenset(
    """
    lower upper substring substr left right trim ltrim rtrim coalesce date year month
    day hour minute second to_char to_date to_timestamp cast concat length char_length
    abs round floor ceil upper lower
    """.split()
)


def _is_literalish(expr: Expr | None) -> bool:
    return isinstance(expr, (Literal, Subquery)) or (
        isinstance(expr, BinOp) and expr.op == "TUPLE"
    )


_NON_SARGABLE_FUNCS = frozenset(
    """
    lower upper substring substr left right trim ltrim rtrim coalesce date year month
    day hour minute second to_char to_date to_timestamp cast concat length char_length
    abs round floor ceil
    """.split()
)


def _is_literalish(expr: Expr | None) -> bool:
    """True for the right-hand side of a predicate the planner can index-probe."""
    return isinstance(expr, (Literal, Subquery)) or (
        isinstance(expr, BinOp) and expr.op == "TUPLE"
    )


def _function_on_column(expr: Expr | None) -> tuple[str, Column] | None:
    """If *expr* is f(column) or CAST(column AS t), return (function, column)."""
    if isinstance(expr, Func) and expr.name.lower() in _NON_SARGABLE_FUNCS:
        inner = expr.args[0] if expr.args else None
        if isinstance(inner, Column):
            return expr.name, inner
    if isinstance(expr, Cast) and isinstance(expr.expr, Column):
        return "CAST", expr.expr
    return None


def _predicate_findings(ctx: FileContext, node: Expr) -> Iterator[Finding]:
    """All predicate-level rules for one expression node (SL005/SL006/SL007).

    One traversal feeds three rules: walking the expression tree once is the
    difference between ~1.5 s and ~0.4 s per megabyte of SQL.
    """
    if not isinstance(node, BinOp):
        return
    op = node.op
    if op in {"=", "<", ">", "<=", ">=", "<>", "!=", "LIKE", "ILIKE"}:
        for side, other in ((node.left, node.right), (node.right, node.left)):
            wrapped = _function_on_column(side)
            if wrapped is None or not _is_literalish(other):
                continue
            func, column = wrapped
            yield finding(
                ctx,
                RULES_BY_CODE["SL005"],
                f"{func}() wraps column {_describe(column)} in a {op} predicate; "
                "an index on the column cannot be used",
                node.line or column.line,
                node.col or column.col,
            )
        if op in {"LIKE", "ILIKE"}:
            pattern = node.right
            if (
                isinstance(pattern, Literal)
                and pattern.kind == "string"
                and (pattern.value.startswith("%") or pattern.value.startswith("_"))
            ):
                yield finding(
                        ctx,
                        RULES_BY_CODE["SL006"],
                        f"{op} '{pattern.value}' on {_describe(node.left)} starts with a "
                        "wildcard, so the index cannot be used",
                        node.line,
                        node.col,
                    )
    elif op == "NOT IN":
        what = "(subquery)" if isinstance(node.right, Subquery) else "a literal list"
        yield finding(
            ctx,
            RULES_BY_CODE["SL007"],
            f"NOT IN over {what} on {_describe(node.left)} returns zero rows "
            "if any value is NULL",
            node.line,
            node.col,
        )


def rule_or_in_join(ctx: FileContext, select: Select) -> Iterator[Finding]:
    for join in select.joins:
        if join.on is None or not _has_or(join.on):
            continue
        yield finding(
            ctx,
            RULES_BY_CODE["SL008"],
            "JOIN condition contains OR, which rules out hash and merge joins: "
            f"{_describe(join.on)}",
            join.on.line or join.line,
            join.on.col or join.col,
        )


def _has_or(expr: Expr | None) -> bool:
    return any(isinstance(n, BinOp) and n.op == "OR" for n in iter_expr(expr))


def rule_order_without_limit(ctx: FileContext, selects: Sequence[Select]) -> Iterator[Finding]:
    for select in selects:
        if not select.order_by:
            continue
        if select.limit is not None or select.offset is not None:
            continue
        first = select.order_by[0]
        yield finding(
            ctx,
            RULES_BY_CODE["SL009"],
            "ORDER BY without LIMIT sorts every matching row; add LIMIT/OFFSET "
            "or a supporting index",
            first.line or select.line,
            first.col,
        )


def rule_cartesian(ctx: FileContext, select: Select) -> Iterator[Finding]:
    comma_sources = [item for item in select.from_items if isinstance(item, TableRef)]
    if len(comma_sources) < 2:
        return
    names = [t.effective_name for t in comma_sources]
    pools = list(names)
    for join in select.joins:
        if isinstance(join.target, TableRef):
            pools.append(join.target.effective_name)
    edges = _reference_edges(select.where, names)
    for join in select.joins:
        edges |= _reference_edges(join.on, [join.target.effective_name, *names])
    groups = _components(names, edges)
    if len(groups) == 1:
        return
    unconnected = ", ".join(sorted(names))
    yield finding(
        ctx,
        RULES_BY_CODE["SL010"],
        f"{len(names)} comma-joined tables ({unconnected}) have no join predicate "
        f"between {len(groups)} groups, so the result is a cartesian product",
        comma_sources[0].line,
        comma_sources[0].col,
    )


def _select_has_aggregate(select: Select) -> bool:
    agg = {"COUNT", "SUM", "AVG", "MIN", "MAX", "ARRAY_AGG", "STRING_AGG", "GROUP_CONCAT"}
    for item in select.columns:
        for node in iter_expr(item.expr):
            if isinstance(node, Func) and node.name.upper() in agg:
                return True
    return False


def rule_redundant_distinct(ctx: FileContext, select: Select) -> Iterator[Finding]:
    if not select.distinct:
        return
    if select.group_by:
        yield finding(
            ctx,
            RULES_BY_CODE["SL011"],
            "DISTINCT together with GROUP BY: grouping already returns unique rows",
            select.line,
            select.col,
        )
    elif _select_has_aggregate(select) and not select.from_items:
        yield finding(
            ctx,
            RULES_BY_CODE["SL011"],
            "DISTINCT over a single aggregate row cannot remove anything",
            select.line,
            select.col,
        )


def rule_unused_cte(ctx: FileContext, stmt: Node, selects: Sequence[Select]) -> Iterator[Finding]:
    if not isinstance(stmt, Select) or not stmt.ctes:
        return
    used: set[str] = set()
    for select in selects:
        for table in select.all_sources:
            used.add(table.name.lower())
    for cte in stmt.ctes:
        inner_used: set[str] = set()
        if cte.query is not None:
            for select in iter_selects(cte.query):
                for table in select.all_sources:
                    inner_used.add(table.name.lower())
        if cte.name.lower() not in used and cte.name.lower() not in inner_used:
            yield finding(
                ctx,
                RULES_BY_CODE["SL012"],
                f"CTE {cte.name} is never referenced by the query",
                cte.line,
                cte.col,
            )


def rule_correlated_subquery(ctx: FileContext, select: Select) -> Iterator[Finding]:
    outer_names = set(_sources(select))
    for expr in _select_expressions(select):
        for sub in expr_subqueries(expr):
            if sub.query is None:
                continue
            inner_names = set()
            for inner_select in iter_selects(sub.query):
                inner_names |= set(_sources(inner_select))
            correlated: list[Column] = []
            for inner_select in iter_selects(sub.query):
                for inner_expr in _select_expressions(inner_select):
                    for column in expr_columns(inner_expr):
                        if column.table and column.table in outer_names:
                            correlated.append(column)
            if not correlated or not inner_names:
                continue
            column = correlated[0]
            yield finding(
                ctx,
                RULES_BY_CODE["SL013"],
                f"subquery reads outer column {_describe(column)} "
                f"({len(correlated)} correlated reference(s)); likely runs once per row",
                sub.line or column.line,
                sub.col or column.col,
            )
            break


def rule_hardcoded_literals(ctx: FileContext, stmt: Node) -> Iterator[Finding]:
    if isinstance(stmt, Update):
        for name, expr in stmt.assignments:
            if isinstance(expr, Literal) and expr.kind in {"string", "number"}:
                yield finding(
                    ctx,
                    RULES_BY_CODE["SL014"],
                    f"UPDATE sets {name} to a literal {expr.value!r}; bind it as a parameter",
                    expr.line or stmt.line,
                    expr.col,
                )
                return
        return
    if isinstance(stmt, Insert):
        for row in stmt.rows:
            for expr in row:
                if isinstance(expr, Literal) and expr.kind in {"string", "number"}:
                    yield finding(
                        ctx,
                        RULES_BY_CODE["SL014"],
                        "INSERT ... VALUES hardcodes literals; use placeholders so the "
                        "statement can be prepared and reused",
                        stmt.line,
                        stmt.col,
                    )
                    return


def _raw_words(stmt: RawStatement) -> list[str]:
    return [t.upper for t in stmt.tokens if t.kind in {WORD}]


def rule_ddl_if_exists(ctx: FileContext, stmt: Node) -> Iterator[Finding]:
    if not isinstance(stmt, RawStatement):
        return
    words = _raw_words(stmt)
    if not words:
        return
    head = words[0]
    if head == "CREATE":
        if "OR" in words[:4] and "REPLACE" in words[:5]:
            return
        if "IF" in words[:4] and "NOT" in words[:5] and "EXISTS" in words[:6]:
            return
        kind = next((w for w in words[1:] if w in {"TABLE", "INDEX", "VIEW", "SCHEMA", "TYPE"}), "")
        yield finding(
            ctx,
            RULES_BY_CODE["SL015"],
            f"CREATE {kind} has no IF NOT EXISTS guard and fails when the object exists",
            stmt.line,
            stmt.col,
        )
    elif head == "DROP":
        if "IF" in words[:4] and "EXISTS" in words[:5]:
            return
        kind = next((w for w in words[1:] if w in {"TABLE", "INDEX", "VIEW", "SCHEMA", "TYPE"}), "")
        yield finding(
            ctx,
            RULES_BY_CODE["SL015"],
            f"DROP {kind} has no IF EXISTS guard and fails when the object is missing",
            stmt.line,
            stmt.col,
        )


def rule_unsafe_type_change(ctx: FileContext, stmt: Node) -> Iterator[Finding]:
    if not isinstance(stmt, RawStatement):
        return
    words = _raw_words(stmt)
    if not words or words[0] != "ALTER" or "TABLE" not in words[:3]:
        return
    if "TYPE" in words or "MODIFY" in words or ("SET" in words and "DATA" in words):
        table = ""
        word_tokens = [t for t in stmt.tokens if t.kind == WORD]
        for i, token in enumerate(word_tokens):
            if token.upper == "TABLE" and i + 1 < len(word_tokens):
                table = word_tokens[i + 1].value
                break
        yield finding(
            ctx,
            RULES_BY_CODE["SL016"],
            f"ALTER TABLE {table} changes a column type, rewriting the table under an "
            "ACCESS EXCLUSIVE lock",
            stmt.line,
            stmt.col,
        )


def rule_missing_schema(ctx: FileContext, stmt: Node, selects: Sequence[Select]) -> Iterator[Finding]:
    ctes = _cte_names(stmt)
    seen: set[str] = set()
    for select in selects:
        for table in select.all_sources:
            key = table.name.lower()
            if table.schema or key in ctes or key in seen:
                continue
            if table.name.startswith("(") or table.name.startswith("$"):
                continue
            seen.add(key)
            yield finding(
                ctx,
                RULES_BY_CODE["SL017"],
                f"table {table.name} is not schema-qualified; resolution depends on search_path",
                table.line,
                table.col,
            )


def _identifiers(stmt: Node, selects: Sequence[Select]) -> Iterator[tuple[str, bool, int, int]]:
    """Yield (name, quoted, line, col) for every identifier in the statement."""
    for select in selects:
        for cte in select.ctes:
            yield cte.name, False, cte.line, cte.col
        for item in select.columns:
            if item.alias:
                yield item.alias, item.alias_quoted, item.line, item.col
            for node in iter_expr(item.expr):
                if isinstance(node, Column):
                    yield node.name, node.quoted, node.line, node.col
                elif isinstance(node, Func):
                    yield node.name, False, node.line, node.col
        for table in select.all_sources:
            yield table.name, table.quoted, table.line, table.col
            if table.schema:
                for part in table.schema.split("."):
                    yield part, False, table.line, table.col
            if table.alias:
                yield table.alias, table.alias_quoted, table.line, table.col
        if select.where is not None:
            for node in iter_expr(select.where):
                if isinstance(node, Column):
                    yield node.name, node.quoted, node.line, node.col
                elif isinstance(node, Func):
                    yield node.name, False, node.line, node.col
    if isinstance(stmt, (Update, Delete)) and stmt.table is not None:
        yield stmt.table.name, stmt.table.quoted, stmt.table.line, stmt.table.col
        if stmt.table.schema:
            for part in stmt.table.schema.split("."):
                yield part, False, stmt.table.line, stmt.table.col
    if isinstance(stmt, Insert) and stmt.table is not None:
        yield stmt.table.name, stmt.table.quoted, stmt.table.line, stmt.table.col


def rule_reserved_word(ctx: FileContext, stmt: Node, selects: Sequence[Select]) -> Iterator[Finding]:
    seen: set[str] = set()
    for name, quoted, line, col in _identifiers(stmt, selects):
        if quoted or not name:
            continue
        key = name.lower()
        if key not in ctx.dialect.reserved:
            continue
        if key in seen:
            continue
        seen.add(key)
        yield finding(
            ctx,
            RULES_BY_CODE["SL018"],
            f"identifier {name} is a reserved word in {ctx.dialect.label}; "
            f"quote it as {ctx.dialect.quote_for(name)} or rename it",
            line,
            col,
        )


_STYLE_NAMES = {"lower": "lowercase", "upper": "uppercase", "title": "Title Case"}


def _keyword_style(word: str) -> str:
    if word.islower():
        return "lower"
    if word.isupper():
        return "upper"
    return "title"


def rule_mixed_case_keywords(ctx: FileContext) -> Iterator[Finding]:
    """Flag a file that writes SQL keywords in more than one casing style."""
    styles: dict[str, list[Token]] = {}
    for tok in ctx.tokens:
        if tok.kind != WORD or tok.quoted:
            continue
        if tok.upper not in KEYWORDS:
            continue
        styles.setdefault(_keyword_style(tok.value), []).append(tok)
    if len(styles) < 2:
        return
    dominant = max(styles, key=lambda key: len(styles[key]))
    for style in sorted(styles):
        if style == dominant:
            continue
        tokens = styles[style]
        first = tokens[0]
        others = ", ".join(sorted({t.value for t in tokens})[:4])
        yield finding(
            ctx,
            RULES_BY_CODE["SL019"],
            f"{len(tokens)} keyword(s) written in {_STYLE_NAMES[style]} "
            f"({others}) while {len(styles[dominant])} use {_STYLE_NAMES[dominant]}",
            first.line,
            first.col,
        )


def rule_trailing_comma(ctx: FileContext) -> Iterator[Finding]:
    tokens = ctx.tokens
    for idx, tok in enumerate(tokens):
        if tok.kind != PUNCT or tok.value != ",":
            continue
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else None
        if nxt is None:
            continue
        if nxt.kind == EOF or (nxt.kind == PUNCT and nxt.value in {")", ";"}):
            yield finding(
                ctx,
                RULES_BY_CODE["SL020"],
                "trailing comma before the end of the clause",
                tok.line,
                tok.col,
            )
            continue
        if nxt.kind == WORD and nxt.upper in CLAUSE_STARTERS:
            yield finding(
                ctx,
                RULES_BY_CODE["SL020"],
                f"trailing comma before {nxt.upper}",
                tok.line,
                tok.col,
            )


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


@dataclass
class LintOptions:
    """Knobs for one lint run. Defaults follow the documented CLI behaviour."""

    dialect: str = "postgres"
    select: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()
    min_severity: Severity = Severity.INFO
    fail_on: Severity = Severity.ERROR


def resolve_rules(options: LintOptions) -> list[Rule]:
    """Apply --select / --ignore and return the enabled rules in code order."""
    wanted = {c.upper() for c in options.select}
    ignored = {c.upper() for c in options.ignore}
    unknown = (wanted | ignored) - set(RULES_BY_CODE)
    if unknown:
        raise ValueError(
            "unknown rule code(s): "
            + ", ".join(sorted(unknown))
            + f" (known codes: {RULES[0].code}-{RULES[-1].code})"
        )
    out = []
    for rule in RULES:
        if wanted and rule.code not in wanted:
            continue
        if rule.code in ignored:
            continue
        out.append(rule)
    return out


def lint_file(
    path: str,
    sql: str,
    options: LintOptions | None = None,
) -> tuple[list[Finding], dict | None]:
    """Lint one file's text; returns (findings, optional parse error record)."""
    options = options or LintOptions()
    dialect = get_dialect(options.dialect)
    try:
        tokens = tokenize(sql, dialect)
    except LexError as exc:
        return [], {
            "path": path,
            "line": exc.line,
            "column": exc.col,
            "message": str(exc),
        }
    try:
        statements = parse(tokens, dialect)
    except ParseError as exc:
        # Token-level rules do not need the AST: still report on them so a
        # file with one broken statement is not silently exempt from style.
        ctx = FileContext(
            path=path, sql=sql, tokens=tokens, statements=[], dialect=dialect
        )
        options_ = options or LintOptions()
        findings = [f for f in rule_trailing_comma(ctx) if f.rule in {
            r.code for r in resolve_rules(options_)
        }]
        findings += [f for f in rule_mixed_case_keywords(ctx) if f.rule in {
            r.code for r in resolve_rules(options_)
        }]
        findings.sort(key=lambda f: (f.line, f.col, f.rule))
        return findings, {
            "path": path,
            "line": exc.line,
            "column": exc.col,
            "message": str(exc),
        }

    ctx = FileContext(path=path, sql=sql, tokens=tokens, statements=statements, dialect=dialect)
    enabled = {r.code for r in resolve_rules(options)}
    min_rank = options.min_severity.rank
    findings: list[Finding] = []
    append = findings.append

    def add(items: Iterable[Finding]) -> None:
        for item in items:
            if item.rule in enabled and item.severity.rank >= min_rank:
                append(item)

    for stmt in statements:
        selects = list(iter_selects(stmt))
        for select in selects:
            add(rule_select_star(ctx, select))
            add(rule_cross_join(ctx, select))
            add(rule_or_in_join(ctx, select))
            add(rule_cartesian(ctx, select))
            add(rule_redundant_distinct(ctx, select))
            add(rule_correlated_subquery(ctx, select))
            for expr in _select_expressions(select):
                for node in iter_expr(expr):
                    add(_predicate_findings(ctx, node))
        if isinstance(stmt, (Update, Delete)) and stmt.where is not None:
            for node in iter_expr(stmt.where):
                add(_predicate_findings(ctx, node))
        if isinstance(stmt, (Update, Delete)):
            add(_dml_missing_where(ctx, stmt))
        add(rule_order_without_limit(ctx, selects))
        add(rule_unused_cte(ctx, stmt, selects))
        add(rule_hardcoded_literals(ctx, stmt))
        add(rule_ddl_if_exists(ctx, stmt))
        add(rule_unsafe_type_change(ctx, stmt))
        add(rule_missing_schema(ctx, stmt, selects))
        add(rule_reserved_word(ctx, stmt, selects))
    add(rule_mixed_case_keywords(ctx))
    add(rule_trailing_comma(ctx))

    findings.sort(key=lambda f: (f.line, f.col, f.rule))
    return findings, None


def lint_text(
    sql: str,
    path: str = "<stdin>",
    options: LintOptions | None = None,
) -> LintReport:
    """Lint a single SQL string and return a report."""
    options = options or LintOptions()
    findings, error = lint_file(path, sql, options)
    return LintReport(
        findings=findings,
        parse_errors=[error] if error else [],
        files=[path],
        lines_scanned=len(sql.splitlines()),
        rules_enabled=[r.code for r in resolve_rules(options)],
        fail_on=options.fail_on,
    )


def lint_sources(
    sources: Sequence[tuple[str, str]],
    options: LintOptions | None = None,
) -> LintReport:
    """Lint several (path, text) pairs into one report."""
    options = options or LintOptions()
    report = LintReport(
        rules_enabled=[r.code for r in resolve_rules(options)],
        fail_on=options.fail_on,
    )
    for path, text in sources:
        findings, error = lint_file(path, text, options)
        report.findings.extend(findings)
        if error:
            report.parse_errors.append(error)
        report.files.append(path)
        report.lines_scanned += len(text.splitlines())
    report.findings.sort(key=lambda f: (f.path, f.line, f.col, f.rule))
    return report


__all__ = [
    "KEYWORDS",
    "RULES",
    "RULES_BY_CODE",
    "FileContext",
    "LintOptions",
    "LintReport",
    "Rule",
    "lint_file",
    "lint_sources",
    "lint_text",
    "resolve_rules",
]