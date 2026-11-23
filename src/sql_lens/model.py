"""Findings, severities and the AST node types shared by every subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Severity of a finding. Ordered least to most serious."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return {"info": 1, "warning": 2, "error": 3}[self.value]

    @classmethod
    def parse(cls, value: str) -> Severity:
        try:
            return cls(value.lower())
        except ValueError as exc:
            raise ValueError(
                f"unknown severity {value!r}; choose one of: info, warning, error"
            ) from exc


SEVERITY_ORDER = (Severity.ERROR, Severity.WARNING, Severity.INFO)


@dataclass(slots=True)
class Finding:
    """One problem found in one file."""

    rule: str
    title: str
    severity: Severity
    message: str
    line: int
    col: int = 1
    path: str = ""
    snippet: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "title": self.title,
            "severity": self.severity.value,
            "message": self.message,
            "line": self.line,
            "column": self.col,
            "path": self.path,
            "snippet": self.snippet,
            "hint": self.hint,
        }


# --------------------------------------------------------------------------- #
# AST nodes. They are deliberately plain dataclasses: no visitors, no magic.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Node:
    line: int = 0
    col: int = 1


@dataclass(slots=True)
class Expr(Node):
    pass


@dataclass(slots=True)
class Column(Expr):
    name: str = ""
    table: str | None = None
    quoted: bool = False


@dataclass(slots=True)
class Star(Expr):
    table: str | None = None


@dataclass(slots=True)
class Literal(Expr):
    kind: str = "string"  # string | number | null | bool | placeholder
    value: str = ""


@dataclass(slots=True)
class Func(Expr):
    name: str = ""
    args: list[Expr] = field(default_factory=list)
    distinct: bool = False
    star: bool = False
    window: str = ""


@dataclass(slots=True)
class BinOp(Expr):
    op: str = ""
    left: Expr | None = None
    right: Expr | None = None


@dataclass(slots=True)
class UnOp(Expr):
    op: str = ""
    operand: Expr | None = None


@dataclass(slots=True)
class Subquery(Expr):
    query: Statement | None = None


@dataclass(slots=True)
class CaseExpr(Expr):
    branches: int = 0


@dataclass(slots=True)
class Cast(Expr):
    expr: Expr | None = None
    type_name: str = ""


@dataclass(slots=True)
class ColumnItem(Node):
    expr: Expr | None = None
    alias: str | None = None
    alias_quoted: bool = False

    @property
    def is_star(self) -> bool:
        return isinstance(self.expr, Star)


@dataclass(slots=True)
class TableRef(Node):
    name: str = ""
    schema: str | None = None
    alias: str | None = None
    quoted: bool = False
    alias_quoted: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    @property
    def effective_name(self) -> str:
        return self.alias or self.name


@dataclass(slots=True)
class SubqueryRef(Node):
    query: Select | None = None
    alias: str | None = None

    @property
    def effective_name(self) -> str:
        return self.alias or ""


@dataclass(slots=True)
class Join(Node):
    kind: str = "INNER"  # INNER | LEFT | RIGHT | FULL | CROSS
    target: TableRef | SubqueryRef | None = None
    on: Expr | None = None
    using: list[str] = field(default_factory=list)
    natural: bool = False


@dataclass(slots=True)
class OrderItem(Node):
    expr: Expr | None = None
    descending: bool = False
    nulls: str = ""


@dataclass(slots=True)
class CTE(Node):
    name: str = ""
    columns: list[str] = field(default_factory=list)
    query: Statement | None = None
    recursive: bool = False


@dataclass(slots=True)
class Select(Node):
    columns: list[ColumnItem] = field(default_factory=list)
    from_items: list[TableRef | SubqueryRef] = field(default_factory=list)
    joins: list[Join] = field(default_factory=list)
    where: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)
    having: Expr | None = None
    order_by: list[OrderItem] = field(default_factory=list)
    limit: Expr | None = None
    offset: Expr | None = None
    distinct: bool = False
    ctes: list[CTE] = field(default_factory=list)
    set_ops: list[tuple[str, Select]] = field(default_factory=list)
    tokens: list[Any] = field(default_factory=list)

    @property
    def all_sources(self) -> list[TableRef]:
        out: list[TableRef] = []
        for item in self.from_items:
            if isinstance(item, TableRef):
                out.append(item)
        for join in self.joins:
            if isinstance(join.target, TableRef):
                out.append(join.target)
        return out


@dataclass(slots=True)
class Insert(Node):
    table: TableRef | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Expr]] = field(default_factory=list)
    select: Select | None = None
    returning: list[ColumnItem] = field(default_factory=list)
    tokens: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class Update(Node):
    table: TableRef | None = None
    assignments: list[tuple[str, Expr]] = field(default_factory=list)
    where: Expr | None = None
    limit: Expr | None = None
    tokens: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class Delete(Node):
    table: TableRef | None = None
    where: Expr | None = None
    limit: Expr | None = None
    tokens: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class RawStatement(Node):
    """Statement we do not model in the AST (DDL, transactions, ...).

    It keeps its tokens so DDL-oriented rules can still inspect it honestly.
    """

    tokens: list[Any] = field(default_factory=list)
    keyword: str = ""

    @property
    def upper_words(self) -> list[str]:
        return [t.upper for t in self.tokens if t.kind in {"WORD", "QUOTED"}]


Statement = Select | Insert | Update | Delete | RawStatement