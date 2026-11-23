"""Complexity scoring: a maintainability grade per query.

The score is a weighted sum of the things reviewers actually complain about:

======================  ======  ============================================
signal                  weight  why it matters
======================  ======  ============================================
join                    3       every join is a new correctness surface
subquery depth          4       nesting is where bugs hide
CTE                     2       indirection costs a reader one hop
predicate               1       each predicate is a branch to reason about
selected column         1       wide payloads are hard to diff
aggregate               2       grouping semantics are subtle
set operation           3       ordering semantics across UNION/EXCEPT
window function         3       frame semantics are subtle
CASE branch             1       branching logic
======================  ======  ============================================

Grades: A <= 8, B <= 16, C <= 26, D <= 40, F above that. The grade is about
*reading* the query, not about how fast it runs - that is what the plan
analyzer is for.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from .dialects import Dialect, get_dialect
from .lint import iter_expr, iter_selects
from .model import (
    BinOp,
    CaseExpr,
    Delete,
    Func,
    Insert,
    Node,
    Select,
    Subquery,
    SubqueryRef,
    Update,
)
from .parser import parse_all

WEIGHTS = {
    "join": 3,
    "subquery_depth": 4,
    "cte": 2,
    "predicate": 1,
    "column": 1,
    "aggregate": 2,
    "set_op": 3,
    "window": 3,
    "case": 1,
}

AGGREGATES = frozenset(
    {"COUNT", "SUM", "AVG", "MIN", "MAX", "ARRAY_AGG", "STRING_AGG", "GROUP_CONCAT", "STDDEV", "VARIANCE"}
)

GRADE_BANDS = ((8, "A"), (16, "B"), (26, "C"), (40, "D"))


def grade_for(score: int) -> str:
    """Map a raw score onto a letter grade."""
    for limit, letter in GRADE_BANDS:
        if score <= limit:
            return letter
    return "F"


@dataclass
class QueryComplexity:
    """Complexity of one statement."""

    label: str
    line: int
    col: int
    joins: int = 0
    subquery_depth: int = 0
    subqueries: int = 0
    ctes: int = 0
    predicates: int = 0
    columns: int = 0
    aggregates: int = 0
    set_ops: int = 0
    windows: int = 0
    cases: int = 0
    score: int = 0
    grade: str = "A"

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "line": self.line,
            "score": self.score,
            "grade": self.grade,
            "joins": self.joins,
            "subquery_depth": self.subquery_depth,
            "subqueries": self.subqueries,
            "ctes": self.ctes,
            "predicates": self.predicates,
            "columns": self.columns,
            "aggregates": self.aggregates,
            "set_ops": self.set_ops,
            "windows": self.windows,
            "cases": self.cases,
        }

    def breakdown(self) -> list[tuple[str, int, int]]:
        """(signal, count, weighted points) for every non-zero signal."""
        rows = [
            ("joins", self.joins, self.joins * WEIGHTS["join"]),
            ("subquery depth", self.subquery_depth, self.subquery_depth * WEIGHTS["subquery_depth"]),
            ("ctes", self.ctes, self.ctes * WEIGHTS["cte"]),
            ("predicates", self.predicates, self.predicates * WEIGHTS["predicate"]),
            ("selected columns", self.columns, self.columns * WEIGHTS["column"]),
            ("aggregates", self.aggregates, self.aggregates * WEIGHTS["aggregate"]),
            ("set operations", self.set_ops, self.set_ops * WEIGHTS["set_op"]),
            ("window functions", self.windows, self.windows * WEIGHTS["window"]),
            ("case branches", self.cases, self.cases * WEIGHTS["case"]),
        ]
        return [row for row in rows if row[1]]


@dataclass
class ComplexityReport:
    """Complexity for every statement in a file."""

    path: str
    queries: list[QueryComplexity] = field(default_factory=list)

    @property
    def worst(self) -> QueryComplexity | None:
        if not self.queries:
            return None
        return max(self.queries, key=lambda q: q.score)

    @property
    def total_score(self) -> int:
        return sum(q.score for q in self.queries)

    @property
    def grade(self) -> str:
        worst = self.worst
        return worst.grade if worst else "A"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "statements": [q.to_dict() for q in self.queries],
            "total_score": self.total_score,
            "worst_grade": self.grade,
        }


def _expr_depth(expr, depth: int = 0) -> int:
    """Deepest subquery nesting inside an expression."""
    best = depth
    if isinstance(expr, Subquery):
        if expr.query is None:
            return best + 1
        return max(best, _select_depth(expr.query, depth + 1))
    if isinstance(expr, BinOp):
        best = max(best, _expr_depth(expr.left, depth), _expr_depth(expr.right, depth))
    elif isinstance(expr, Func):
        for arg in expr.args:
            best = max(best, _expr_depth(arg, depth))
    return best


def _select_depth(select: Select, depth: int = 0) -> int:
    best = depth
    for item in select.columns:
        best = max(best, _expr_depth(item.expr, depth))
    if select.where is not None:
        best = max(best, _expr_depth(select.where, depth))
    for join in select.joins:
        best = max(best, _expr_depth(join.on, depth))
    for source in select.from_items:
        if isinstance(source, SubqueryRef) and source.query is not None:
            best = max(best, _select_depth(source.query, depth + 1))
    for join in select.joins:
        if isinstance(join.target, SubqueryRef) and join.target.query is not None:
            best = max(best, _select_depth(join.target.query, depth + 1))
    for _, right in select.set_ops:
        best = max(best, _select_depth(right, depth))
    return best


def _count_subqueries(select: Select) -> int:
    total = 0
    for item in select.columns:
        total += sum(1 for _ in _walk_subqueries(item.expr))
    if select.where is not None:
        total += sum(1 for _ in _walk_subqueries(select.where))
    for join in select.joins:
        if join.on is not None:
            total += sum(1 for _ in _walk_subqueries(join.on))
        if isinstance(join.target, SubqueryRef):
            total += 1
    for source in select.from_items:
        if isinstance(source, SubqueryRef):
            total += 1
    return total


def _walk_subqueries(expr) -> Iterator[Subquery]:
    stack = [expr]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if isinstance(node, Subquery):
            yield node
            continue
        if isinstance(node, BinOp):
            stack.extend([node.left, node.right])
        elif isinstance(node, Func):
            stack.extend(node.args)


def _count_predicates(expr) -> int:
    if expr is None:
        return 0
    total = 0
    for node in iter_expr(expr):
        if (isinstance(node, BinOp) and node.op in {
            "AND", "OR", "=", "<", ">", "<=", ">=", "<>", "!=", "LIKE", "ILIKE",
            "NOT LIKE", "NOT ILIKE", "IN", "NOT IN", "IS", "BETWEEN",
        }) or (isinstance(node, Func) and node.name.upper() == "EXISTS"):
            total += 1
    return total


def _count_columns(select: Select) -> int:
    return sum(1 for item in select.columns if not item.is_star)


def _label_for(stmt: Node, index: int) -> str:
    if isinstance(stmt, Select):
        tables = [t.name for t in stmt.all_sources[:3]]
        if stmt.ctes:
            base = "WITH query"
        elif tables:
            base = "query on " + ", ".join(tables)
        else:
            base = "query"
        return f"#{index} {base}"
    if isinstance(stmt, Insert):
        return f"#{index} INSERT"
    if isinstance(stmt, Update):
        return f"#{index} UPDATE"
    if isinstance(stmt, Delete):
        return f"#{index} DELETE"
    return f"#{index} statement"


def score_complexity(node: Node, index: int = 1, label: str | None = None) -> QueryComplexity:
    """Score one statement."""
    result = QueryComplexity(
        label=label or _label_for(node, index),
        line=getattr(node, "line", 0),
        col=getattr(node, "col", 1),
    )
    selects = list(iter_selects(node))
    for select in selects:
        result.joins += len(select.joins)
        result.ctes += len(select.ctes)
        result.set_ops += len(select.set_ops)
        result.subqueries += _count_subqueries(select)
        result.subquery_depth = max(result.subquery_depth, _select_depth(select))
        result.predicates += _count_predicates(select.where)
        result.columns += _count_columns(select)
        for item in select.columns:
            if isinstance(item.expr, Func):
                name = item.expr.name.upper()
                if name in AGGREGATES:
                    result.aggregates += 1
                if item.expr.window:
                    result.windows += 1
            for sub in iter_expr(item.expr):
                if isinstance(sub, Func) and sub.window:
                    result.windows += 1
                if isinstance(sub, CaseExpr):
                    result.cases += sub.branches
    if not selects and isinstance(node, (Update, Delete)):
        result.predicates += _count_predicates(node.where)
    result.score = (
        result.joins * WEIGHTS["join"]
        + result.subquery_depth * WEIGHTS["subquery_depth"]
        + result.ctes * WEIGHTS["cte"]
        + result.predicates * WEIGHTS["predicate"]
        + result.columns * WEIGHTS["column"]
        + result.aggregates * WEIGHTS["aggregate"]
        + result.set_ops * WEIGHTS["set_op"]
        + result.windows * WEIGHTS["window"]
        + result.cases * WEIGHTS["case"]
    )
    result.grade = grade_for(result.score)
    return result


def score_text(sql: str, dialect: str | Dialect = "postgres", path: str = "<stdin>") -> ComplexityReport:
    """Score every statement in *sql*."""
    dia = get_dialect(dialect) if isinstance(dialect, str) else dialect
    statements = parse_all(sql, dia)
    report = ComplexityReport(path=path)
    for index, stmt in enumerate(statements, start=1):
        report.queries.append(score_complexity(stmt, index))
    return report


__all__ = [
    "AGGREGATES",
    "GRADE_BANDS",
    "WEIGHTS",
    "ComplexityReport",
    "QueryComplexity",
    "grade_for",
    "score_complexity",
    "score_text",
]