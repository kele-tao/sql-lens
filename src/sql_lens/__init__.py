"""sql-lens: static analysis for SQL without a database.

The public API is deliberately small. Everything the CLI does is available as a
function so that other tools can embed sql-lens:

    from sql_lens import lint_text, format_sql, analyse_plan, score_text
"""

from __future__ import annotations

from .complexity import ComplexityReport, QueryComplexity, score_complexity, score_text
from .dialects import DIALECTS, Dialect, get_dialect
from .formatter import format_sql
from .lexer import Token, tokenize
from .lint import LintReport, lint_text
from .model import Finding, Severity
from .parser import ParseError, parse, parse_all, parse_one
from .plan import PlanAnalysis, analyse_plan

__version__ = "1.0.0"

__all__ = [
    "DIALECTS",
    "ComplexityReport",
    "Dialect",
    "Finding",
    "LintReport",
    "ParseError",
    "PlanAnalysis",
    "QueryComplexity",
    "Severity",
    "Token",
    "__version__",
    "analyse_plan",
    "format_sql",
    "get_dialect",
    "lint_text",
    "parse",
    "parse_all",
    "parse_one",
    "score_complexity",
    "score_text",
    "tokenize",
]
