"""EXPLAIN (ANALYZE, FORMAT JSON) diagnosis.

The input is the real JSON PostgreSQL produces, either as a raw JSON array or as
a file where the JSON sits inside one text column. Nothing here needs a
database connection: the plan is a tree of observed costs, and reading it well
is the hard part.

What the analyzer detects
-------------------------
================================  ===============================================
code  meaning                      why it matters
================================  ===============================================
SLP001  sequential scan             full table read; usually an index or a rewrite
SLP002  nested loop with many rows   O(outer x inner) work, not O(n log n)
SLP003  estimate off by 100x        the planner chose a plan for the wrong data
SLP004  sort/hash spill to disk      memory (work_mem) is too small for the sort
SLP005  filter rejecting most rows   an index would skip the rejected tuples
================================  ===============================================

Index suggestions
-----------------
Filter and join conditions of the expensive nodes are parsed for column names,
then ranked by the number of rows they discard. A suggestion is only emitted if
the plan proves the column was actually used in a predicate, and the DDL is
spelled out so a reviewer can copy it into a migration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .model import Severity

DEFAULT_LARGE_ROWS = 5000
DEFAULT_NESTED_LOOP_ROWS = 10000
DEFAULT_ESTIMATE_FACTOR = 100.0
DEFAULT_ESTIMATE_FLOOR = 1000.0
MAX_SUGGESTIONS = 3

_IDENT = r'(\w+|"[^"]+"|[A-Za-z_][\w$]*)'
_COLUMN_PATTERNS = (
    re.compile(_IDENT + r"\s*(?:=|<>|!=|>=|<=|>|<|~~|~~\*|!~~)\s*(?:\$\d+|'|-?\d|NULL)", re.I),
    re.compile(r"\(\s*" + _IDENT + r"\s*\)::\w+", re.I),
    re.compile(r"lower\(\s*" + _IDENT, re.I),
    re.compile(r"upper\(\s*" + _IDENT, re.I),
    re.compile(r"date\(\s*" + _IDENT, re.I),
)
_JOIN_KEY = re.compile(r"\(\s*([\w.]+)\s*=\s*([\w.]+)\s*\)")
_ANY_KEY = re.compile(r"([\w.]+)\s*=\s*([\w.]+)")
_SQL_WORDS = {
    "and", "or", "not", "null", "true", "false", "is", "any", "all", "in", "like",
    "similar", "between", "case", "when", "then", "else", "end", "array", "subplan",
    "now", "current_date", "current_timestamp",
}


class PlanError(ValueError):
    """Raised when the input is not a readable EXPLAIN JSON document."""


@dataclass
class PlanFinding:
    """One diagnosed problem in a plan."""

    code: str
    severity: Severity
    title: str
    message: str
    path: str = ""
    relation: str = ""
    node_type: str = ""
    hint: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "relation": self.relation,
            "node_type": self.node_type,
            "path": self.path,
            "hint": self.hint,
        }


@dataclass
class IndexSuggestion:
    """A CREATE INDEX statement justified by the plan."""

    table: str
    columns: list[str]
    reason: str
    rows_discarded: int
    ddl: str

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "columns": self.columns,
            "reason": self.reason,
            "rows_discarded": self.rows_discarded,
            "ddl": self.ddl,
        }


@dataclass
class PlanNode:
    """The subset of a PostgreSQL plan node we actually analyse."""

    node_type: str
    relation: str = ""
    alias: str = ""
    path: str = ""
    parent: PlanNode | None = field(default=None, repr=False, compare=False)
    plan_rows: float = 0.0
    actual_rows: float = 0.0
    actual_loops: float = 1.0
    total_cost: float = 0.0
    actual_total_time: float = 0.0
    rows_removed: float = 0.0
    filter: str = ""
    index_cond: str = ""
    hash_cond: str = ""
    join_type: str = ""
    sort_method: str = ""
    sort_space_used: int = 0
    sort_key: list[str] = field(default_factory=list)
    children: list[PlanNode] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def effective_rows(self) -> float:
        """Rows produced across all loops."""
        return self.actual_rows * max(self.actual_loops, 1.0)

    @property
    def estimate_error(self) -> float:
        """Actual / planned rows, or 0.0 when the planner expected no rows."""
        if self.plan_rows <= 0:
            return 0.0
        return self.effective_rows / self.plan_rows

    @property
    def qualified_relation(self) -> str:
        return self.alias or self.relation


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_node(data: dict[str, Any], path: str, parent: PlanNode | None = None) -> PlanNode:
    node = PlanNode(
        node_type=str(data.get("Node Type", "Unknown")),
        relation=str(data.get("Relation Name", "")),
        alias=str(data.get("Alias", "")),
        path=f"{path}/{data.get('Relation Name') or data.get('Node Type', 'Node')}",
        parent=parent,
        plan_rows=_as_float(data.get("Plan Rows")),
        actual_rows=_as_float(data.get("Actual Rows")),
        actual_loops=_as_float(data.get("Actual Loops"), 1.0),
        total_cost=_as_float(data.get("Total Cost")),
        actual_total_time=_as_float(data.get("Actual Total Time")),
        rows_removed=_as_float(data.get("Rows Removed by Filter"))
        + _as_float(data.get("Rows Removed by Join Filter")),
        filter=str(data.get("Filter", "")),
        index_cond=str(data.get("Index Cond", "")),
        hash_cond=str(data.get("Hash Cond", "")),
        join_type=str(data.get("Join Type", "")),
        sort_method=str(data.get("Sort Method", "")),
        sort_space_used=_as_int(data.get("Sort Space Used")),
        sort_key=[str(k) for k in data.get("Sort Key", [])],
        raw=data,
    )
    for child in data.get("Plans", []) or []:
        node.children.append(_build_node(child, node.path, node))
    return node


def _find_plan(doc: Any) -> dict[str, Any]:
    """Accept the shapes PostgreSQL and psql actually emit."""
    if isinstance(doc, list):
        if not doc:
            raise PlanError("the EXPLAIN document contains no query plan")
        return _find_plan(doc[0])
    if isinstance(doc, dict):
        if "Plan" in doc and isinstance(doc["Plan"], dict):
            return doc["Plan"]
        if "Node Type" in doc:
            return doc
        # psql -A -t output: {"QUERY PLAN": [{...}]}
        for value in doc.values():
            if isinstance(value, (list, dict)):
                try:
                    return _find_plan(value)
                except PlanError:
                    continue
    raise PlanError(
        "could not find a query plan; pass the output of "
        "EXPLAIN (ANALYZE, FORMAT JSON) SELECT ..."
    )


def load_plan(text: str) -> tuple[PlanNode, dict[str, Any]]:
    """Parse EXPLAIN JSON *text* into (root node, raw document)."""
    stripped = text.strip()
    if not stripped:
        raise PlanError("empty plan document")
    try:
        doc = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise PlanError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}. "
            "Use EXPLAIN (ANALYZE, FORMAT JSON) and save the whole output."
        ) from exc
    plan = _find_plan(doc)
    root = _build_node(plan, "")
    summary = doc[0] if isinstance(doc, list) and doc and isinstance(doc[0], dict) else doc
    return root, summary if isinstance(summary, dict) else {}


def walk_plan(node: PlanNode) -> Iterable[PlanNode]:
    """Depth-first over every plan node."""
    yield node
    for child in node.children:
        yield from walk_plan(child)


def _is_seq_scan(node: PlanNode) -> bool:
    return node.node_type.startswith("Seq Scan")


def _is_nested_loop(node: PlanNode) -> bool:
    return "Nested Loop" in node.node_type


def _is_sort(node: PlanNode) -> bool:
    return "Sort" in node.node_type and "Incremental" not in node.node_type


_FUNC_WRAPPERS = re.compile(r"\b(lower|upper|date|cast|substring|trim|to_char|extract)\s*\(", re.I)


def _clean_identifier(raw: str) -> str:
    name = raw.strip().strip('"')
    if "." in name:
        name = name.split(".")[-1].strip('"')
    return name


def extract_columns(predicate: str) -> list[str]:
    """Extract candidate column names from a plan predicate string."""
    if not predicate:
        return []
    found: list[str] = []
    for pattern in _COLUMN_PATTERNS:
        for match in pattern.finditer(predicate):
            name = _clean_identifier(match.group(1))
            if not name or name.lower() in _SQL_WORDS:
                continue
            if name not in found:
                found.append(name)
    for match in _JOIN_KEY.finditer(predicate):
        left, right = match.group(1), match.group(2)
        if "." in left and "." not in right:
            found.append(_clean_identifier(left))
        elif "." in right and "." not in left:
            found.append(_clean_identifier(right))
    # drop anything that is a table alias only or a pure number
    return [c for c in found if not c.isdigit()]


def _table_name(node: PlanNode) -> str:
    return node.relation or node.alias or "unknown_table"


def _collect_index_candidates(root: PlanNode) -> list[IndexSuggestion]:
    candidates: dict[tuple[str, tuple[str, ...]], IndexSuggestion] = {}
    for node in walk_plan(root):
        if not _is_seq_scan(node) or not node.relation:
            continue
        columns = extract_columns(node.filter)
        for column in columns:
            key = (node.relation, (column,))
            discarded = int(node.rows_removed)
            reason = (
                f"Seq Scan on {node.relation} removed {discarded:,} rows via "
                f"{node.filter.strip() or 'a filter'}"
            )
            existing = candidates.get(key)
            if existing is None or discarded > existing.rows_discarded:
                candidates[key] = IndexSuggestion(
                    table=node.relation,
                    columns=[column],
                    reason=reason,
                    rows_discarded=discarded,
                    ddl=f"CREATE INDEX ON {node.relation} ({column});",
                )
        # join-side suggestion: a nested loop inner table scanned without index
        if node.parent is not None and _is_nested_loop(node.parent):
            columns = extract_columns(node.filter) or extract_columns(node.parent.raw.get("Join Filter", ""))
            for column in columns:
                key = (node.relation, (column,))
                if key in candidates:
                    continue
                candidates[key] = IndexSuggestion(
                    table=node.relation,
                    columns=[column],
                    reason=(
                        f"inner side of a Nested Loop scans {node.relation} "
                        f"{int(node.actual_loops):,} times"
                    ),
                    rows_discarded=int(node.effective_rows),
                    ddl=f"CREATE INDEX ON {node.relation} ({column});",
                )
    ranked = sorted(
        candidates.values(), key=lambda s: (-s.rows_discarded, s.table, s.columns)
    )
    return ranked[:MAX_SUGGESTIONS]


def _findings_for_node(node: PlanNode, large_rows: int) -> list[PlanFinding]:
    out: list[PlanFinding] = []
    if _is_seq_scan(node) and node.effective_rows >= large_rows:
        out.append(
            PlanFinding(
                code="SLP001",
                severity=Severity.WARNING,
                title="sequential scan over many rows",
                message=(
                    f"Seq Scan on {_table_name(node)} returned {int(node.effective_rows):,} rows"
                    + (
                        f" and removed {int(node.rows_removed):,} with a filter"
                        if node.rows_removed
                        else ""
                    )
                ),
                path=node.path,
                relation=_table_name(node),
                node_type=node.node_type,
                hint="Add an index for the filter/join columns, or select fewer columns.",
            )
        )
    if _is_nested_loop(node) and node.effective_rows >= DEFAULT_NESTED_LOOP_ROWS:
        out.append(
            PlanFinding(
                code="SLP002",
                severity=Severity.ERROR,
                title="nested loop producing a large result",
                message=(
                    f"Nested Loop produced {int(node.effective_rows):,} rows with "
                    f"{int(node.actual_loops):,} loops; cost grows with outer x inner"
                ),
                path=node.path,
                relation=_table_name(node),
                node_type=node.node_type,
                hint="Check for a missing join index or a bad row estimate feeding this loop.",
            )
        )
    if node.plan_rows > 0 and node.actual_rows >= 0:
        error = node.estimate_error
        # Tiny row counts produce huge ratios that mean nothing: suppress them.
        noisy = max(node.effective_rows, node.plan_rows) < DEFAULT_ESTIMATE_FLOOR
        if not noisy and (error >= DEFAULT_ESTIMATE_FACTOR or 0 < error <= 1 / DEFAULT_ESTIMATE_FACTOR):
            factor = f"{error:,.0f}x more" if error >= 1 else f"{1 / error:,.0f}x fewer"
            out.append(
                PlanFinding(
                    code="SLP003",
                    severity=Severity.WARNING,
                    title="row estimate off by more than 100x",
                    message=(
                        f"{node.node_type} planned {int(node.plan_rows):,} rows but saw "
                        f"{int(node.effective_rows):,} ({factor})"
                    ),
                    path=node.path,
                    relation=_table_name(node),
                    node_type=node.node_type,
                    hint="Run ANALYZE, or check whether the predicate is correlated across columns.",
                )
            )
    if _is_sort(node) and "external" in node.sort_method.lower():
        out.append(
            PlanFinding(
                code="SLP004",
                severity=Severity.WARNING,
                title="sort spilled to disk",
                message=(
                    f"{node.node_type} used {node.sort_space_used:,} kB on disk "
                    f"({node.sort_method}); the result did not fit in work_mem"
                ),
                path=node.path,
                node_type=node.node_type,
                hint="Raise work_mem for this query, or add an index matching the sort order.",
            )
        )
    if _is_seq_scan(node) and node.rows_removed >= max(large_rows // 2, 1):
        out.append(
            PlanFinding(
                code="SLP005",
                severity=Severity.INFO,
                title="filter discards most of the scanned rows",
                message=(
                    f"Seq Scan on {_table_name(node)} removed {int(node.rows_removed):,} rows "
                    f"and kept {int(node.effective_rows):,}"
                ),
                path=node.path,
                relation=_table_name(node),
                node_type=node.node_type,
                hint="A selective index on the filter column would skip the rejected rows.",
            )
        )
    return out


@dataclass
class PlanAnalysis:
    """Result of analysing one EXPLAIN document."""

    path: str = ""
    planning_time: float = 0.0
    execution_time: float = 0.0
    node_count: int = 0
    total_actual_rows: float = 0.0
    root: PlanNode | None = None
    findings: list[PlanFinding] = field(default_factory=list)
    suggestions: list[IndexSuggestion] = field(default_factory=list)

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    def has_blockers(self) -> bool:
        return self.count(Severity.ERROR) > 0

    def to_dict(self) -> dict:
        return {
            "tool": "sql-lens",
            "version": "1.0.0",
            "plan": self.path or "<plan>",
            "planning_time_ms": round(self.planning_time, 3),
            "execution_time_ms": round(self.execution_time, 3),
            "node_count": self.node_count,
            "summary": {
                "findings": len(self.findings),
                "errors": self.count(Severity.ERROR),
                "warnings": self.count(Severity.WARNING),
                "info": self.count(Severity.INFO),
            },
            "findings": [f.to_dict() for f in self.findings],
            "suggested_indexes": [s.to_dict() for s in self.suggestions],
        }


def analyse_plan(text: str, path: str = "", large_rows: int = DEFAULT_LARGE_ROWS) -> PlanAnalysis:
    """Analyse an EXPLAIN (ANALYZE, FORMAT JSON) document."""
    if large_rows < 1:
        raise ValueError("--large-rows must be a positive integer")
    root, doc = load_plan(text)
    analysis = PlanAnalysis(path=path, root=root)
    analysis.planning_time = _as_float(doc.get("Planning Time"))
    analysis.execution_time = _as_float(doc.get("Execution Time"))
    nodes = list(walk_plan(root))
    analysis.node_count = len(nodes)
    analysis.total_actual_rows = root.effective_rows
    for node in nodes:
        analysis.findings.extend(_findings_for_node(node, large_rows))
    analysis.findings.sort(key=lambda f: (-f.severity.rank, f.code, f.path))
    analysis.suggestions = _collect_index_candidates(root)
    return analysis


__all__ = [
    "DEFAULT_ESTIMATE_FACTOR",
    "DEFAULT_ESTIMATE_FLOOR",
    "DEFAULT_LARGE_ROWS",
    "DEFAULT_NESTED_LOOP_ROWS",
    "IndexSuggestion",
    "PlanAnalysis",
    "PlanError",
    "PlanFinding",
    "PlanNode",
    "analyse_plan",
    "extract_columns",
    "load_plan",
    "walk_plan",
]