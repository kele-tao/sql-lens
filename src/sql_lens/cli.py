"""The sql-lens command line interface.

Commands
--------
  sql-lens lint <paths...>       lint .sql files or stdin
  sql-lens plan <plan.json>      diagnose EXPLAIN (ANALYZE, FORMAT JSON)
  sql-lens format <paths...>     deterministic formatter (--check/--write)
  sql-lens complexity <paths...> per-query complexity score and grade
  sql-lens rules                 the rule reference (SL001-SL020)
  sql-lens dialects              quoting/limit style per SQL dialect

Exit codes (documented in the README)
  0  success, nothing failed the configured gate
  1  findings at or above --fail-on, or --check found unformatted files
  2  usage error: bad flag, unreadable file, unparseable SQL
"""

from __future__ import annotations

import argparse
import glob as globlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .complexity import score_text
from .dialects import DIALECTS
from .formatter import format_sql
from .lexer import LexError
from .lint import RULES, LintOptions, lint_sources
from .model import Severity
from .parser import ParseError
from .plan import PlanError, analyse_plan
from .render import (
    colour_enabled,
    render_complexity_text,
    render_html,
    render_markdown,
    render_plan_text,
    render_text,
)

PROG = "sql-lens"
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

SQL_SUFFIXES = (".sql", ".psql", ".ddl", ".dml")
MAX_FILE_BYTES = 8 * 1024 * 1024


class UsageError(Exception):
    """A problem with the invocation, reported without a traceback."""


def _eprint(message: str) -> None:
    print(message, file=sys.stderr)


def _read_text(path: Path) -> str:
    if not path.exists():
        raise UsageError(f"no such file: {path}")
    if path.is_dir():
        raise UsageError(f"{path} is a directory; pass files or a glob such as '{path}/*.sql'")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise UsageError(
            f"{path} is {size / 1048576:.1f} MB, above the {MAX_FILE_BYTES // 1048576} MB limit"
        )
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{path} is not valid UTF-8 ({exc.reason}); sql-lens only reads UTF-8") from exc
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror or exc}") from exc


def _expand(patterns: Sequence[str]) -> list[Path]:
    """Expand globs, de-duplicate and keep the user's order."""
    out: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        if pattern == "-":
            raise UsageError("use --stdin to read SQL from standard input")
        if globlib.has_magic(pattern):
            matches = sorted(globlib.glob(pattern, recursive=True))
            if not matches:
                raise UsageError(f"pattern matched no files: {pattern}")
            candidates = [Path(m) for m in matches]
        else:
            candidates = [Path(pattern)]
        for candidate in candidates:
            if candidate.is_dir():
                found = sorted(
                    str(p) for suffix in SQL_SUFFIXES for p in candidate.rglob(f"*{suffix}")
                )
                if not found:
                    raise UsageError(f"directory {candidate} contains no .sql files")
                for name in found:
                    key = os.path.normcase(os.path.abspath(name))
                    if key not in seen:
                        seen.add(key)
                        out.append(Path(name))
                continue
            key = os.path.normcase(os.path.abspath(str(candidate)))
            if key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


def _load_sources(paths: Sequence[str], use_stdin: bool) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    if use_stdin or not paths:
        data = sys.stdin.read() if use_stdin else ""
        if not data.strip():
            raise UsageError(
                "no input: pass one or more .sql files, a glob, or --stdin with SQL on stdin"
            )
        sources.append(("<stdin>", data))
        return sources
    for path in _expand(paths):
        sources.append((str(path), _read_text(path)))
    return sources


def _write_output(text: str, output: str | None) -> None:
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target} ({len(text)} bytes)")
        return
    sys.stdout.write(text)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_lint(args: argparse.Namespace) -> int:
    try:
        options = LintOptions(
            dialect=args.dialect,
            select=tuple(args.select or ()),
            ignore=tuple(args.ignore or ()),
            min_severity=Severity.parse(args.min_severity),
            fail_on=Severity.parse(args.fail_on),
        )
        sources = _load_sources(args.paths, args.stdin)
        report = lint_sources(sources, options)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc

    fmt = args.format
    if fmt == "json":
        _write_output(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", args.output)
    elif fmt == "html":
        _write_output(render_html(report), args.output)
    elif fmt == "markdown":
        _write_output(render_markdown(report), args.output)
    else:
        _write_output(render_text(report, color=colour_enabled() and not args.no_color), args.output)

    if args.statistics:
        _eprint(
            f"{len(report.findings)} findings in {len(report.files)} files, "
            f"{report.lines_scanned} lines, {len(report.rules_enabled)} rules"
        )
    return EXIT_FINDINGS if report.failed() else EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        text = sys.stdin.read() if args.stdin else _read_text(Path(args.plan_file))
        analysis = analyse_plan(text, path="" if args.stdin else args.plan_file, large_rows=args.large_rows)
    except PlanError as exc:
        raise UsageError(str(exc)) from exc
    except ValueError as exc:
        raise UsageError(str(exc)) from exc

    if args.format == "json":
        _write_output(json.dumps(analysis.to_dict(), indent=2, ensure_ascii=False) + "\n", args.output)
    else:
        _write_output(
            render_plan_text(analysis, color=colour_enabled() and not args.no_color), args.output
        )
    if args.fail_on_any and analysis.findings:
        return EXIT_FINDINGS
    return EXIT_FINDINGS if analysis.has_blockers() else EXIT_OK


def cmd_format(args: argparse.Namespace) -> int:
    try:
        sources = _load_sources(args.paths, args.stdin)
    except UsageError:
        raise
    changed: list[str] = []
    rendered: dict[str, str] = {}
    try:
        for path, text in sources:
            try:
                formatted = format_sql(text, args.dialect)
            except (ParseError, LexError) as exc:
                raise UsageError(f"{path}: cannot format, the SQL does not parse: {exc}") from exc
            rendered[path] = formatted
            if formatted != text:
                changed.append(path)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc

    if args.check:
        if changed:
            for path in changed:
                _eprint(f"would reformat {path}")
            return EXIT_FINDINGS
        print(f"{len(sources)} file(s) already formatted")
        return EXIT_OK

    if args.write:
        for path, text in rendered.items():
            if path == "<stdin>":
                sys.stdout.write(text)
                continue
            if text != dict(sources)[path]:
                Path(path).write_text(text, encoding="utf-8")
                _eprint(f"reformatted {path}")
        return EXIT_OK

    if args.paths and len(rendered) > 1:
        sys.stdout.write("\n".join(rendered[p] for p, _ in sources if p in rendered))
    else:
        for _path, text in rendered.items():
            sys.stdout.write(text)
    return EXIT_OK


def cmd_complexity(args: argparse.Namespace) -> int:
    try:
        sources = _load_sources(args.paths, args.stdin)
    except UsageError:
        raise
    reports = []
    for path, text in sources:
        try:
            reports.append(score_text(text, args.dialect, path=path))
        except (ParseError, LexError) as exc:
            raise UsageError(f"{path}: cannot score, the SQL does not parse: {exc}") from exc

    if args.format == "json":
        payload = {
            "tool": "sql-lens",
            "version": __version__,
            "reports": [r.to_dict() for r in reports],
        }
        _write_output(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", args.output)
    else:
        text_out = "".join(
            render_complexity_text(r, color=colour_enabled() and not args.no_color) for r in reports
        )
        _write_output(text_out, args.output)

    if args.max_grade:
        worst = args.max_grade.upper()
        order = "ABCDF"
        if worst not in order:
            raise UsageError(f"--max-grade must be one of A, B, C, D, F (got {args.max_grade!r})")
        limit = order.index(worst)
        for report in reports:
            for query in report.queries:
                if order.index(query.grade) > limit:
                    return EXIT_FINDINGS
    return EXIT_OK


def cmd_rules(args: argparse.Namespace) -> int:
    if args.format == "json":
        payload = [
            {
                "code": r.code,
                "name": r.name,
                "severity": r.severity.value,
                "summary": r.summary,
                "hint": r.hint,
                "example": r.example,
            }
            for r in RULES
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return EXIT_OK
    if args.format == "markdown":
        lines = ["| code | rule | severity | catches |", "| --- | --- | --- | --- |"]
        for r in RULES:
            lines.append(f"| {r.code} | {r.name} | {r.severity.value} | {r.summary} |")
        print("\n".join(lines))
        return EXIT_OK
    print(f"{'code':<7}{'severity':<10}{'rule':<26}summary")
    print("-" * 100)
    for r in RULES:
        print(f"{r.code:<7}{r.severity.value:<10}{r.name:<26}{r.summary}")
    print()
    print(f"{len(RULES)} rules. Run 'sql-lens rules --format markdown' for the README table.")
    return EXIT_OK


def cmd_dialects(args: argparse.Namespace) -> int:
    print(f"{'name':<11}{'label':<20}{'row limit':<14}notes")
    print("-" * 100)
    for name, dialect in DIALECTS.items():
        print(f"{name:<11}{dialect.label:<20}{dialect.limit_style:<14}{', '.join(dialect.notes)}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def _severity(value: str) -> str:
    return Severity.parse(value).value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Static analysis for SQL: lint rules, EXPLAIN-plan diagnosis, "
        "a deterministic formatter and complexity scoring. No database needed.",
        epilog="Exit codes: 0 ok, 1 findings/quality gate failed, 2 usage error. "
        "Docs: https://github.com/kele-tao/sql-lens",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    lint = sub.add_parser("lint", help="lint SQL files")
    lint.add_argument("paths", nargs="*", help="files, directories or globs (default: --stdin)")
    lint.add_argument("--stdin", action="store_true", help="read SQL from standard input")
    lint.add_argument("--dialect", default="postgres", help="postgres (default), mysql, sqlite, "
                       "sqlserver, ansi")
    lint.add_argument("--json", dest="legacy_json", action="store_true", help=argparse.SUPPRESS)
    lint.add_argument("--format", choices=["text", "json", "html", "markdown"], default="text",
                      help="report format (default: text)")
    lint.add_argument("--output", "-o", help="write the report to a file instead of stdout")
    lint.add_argument("--select", action="append", metavar="CODE",
                      help="only run these rule codes (repeatable)")
    lint.add_argument("--ignore", action="append", metavar="CODE",
                      help="skip these rule codes (repeatable)")
    lint.add_argument("--min-severity", type=_severity, default="info",
                      help="hide findings below this severity (default: info)")
    lint.add_argument("--fail-on", type=_severity, default="error",
                      help="exit 1 at or above this severity (default: error)")
    lint.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    lint.add_argument("--statistics", action="store_true", help="print a summary line to stderr")
    lint.set_defaults(func=cmd_lint)

    plan = sub.add_parser("plan", help="diagnose an EXPLAIN (ANALYZE, FORMAT JSON) document")
    plan.add_argument("plan_file", nargs="?", help="path to the JSON plan")
    plan.add_argument("--stdin", action="store_true", help="read the plan from standard input")
    plan.add_argument("--json", dest="json_flag", action="store_true", help=argparse.SUPPRESS)
    plan.add_argument("--format", choices=["text", "json"], default="text")
    plan.add_argument("--output", "-o", help="write the report to a file")
    plan.add_argument("--large-rows", type=int, default=5000,
                      help="row count that makes a sequential scan worth flagging (default: 5000)")
    plan.add_argument("--fail-on-any", action="store_true",
                      help="exit 1 on any finding, not only errors")
    plan.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    plan.set_defaults(func=cmd_plan)

    fmt = sub.add_parser("format", help="format SQL deterministically")
    fmt.add_argument("paths", nargs="*", help="files or globs (default: --stdin)")
    fmt.add_argument("--stdin", action="store_true", help="read SQL from standard input")
    fmt.add_argument("--dialect", default="postgres")
    fmt.add_argument("--check", action="store_true", help="exit 1 if any file would change")
    fmt.add_argument("--write", "-w", action="store_true", help="rewrite the files in place")
    fmt.set_defaults(func=cmd_format)

    cx = sub.add_parser("complexity", help="score query complexity and grade maintainability")
    cx.add_argument("paths", nargs="*", help="files or globs (default: --stdin)")
    cx.add_argument("--stdin", action="store_true", help="read SQL from standard input")
    cx.add_argument("--dialect", default="postgres")
    cx.add_argument("--format", choices=["text", "json"], default="text")
    cx.add_argument("--output", "-o", help="write the report to a file")
    cx.add_argument("--max-grade", help="exit 1 when a query scores worse than this grade (A-F)")
    cx.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    cx.set_defaults(func=cmd_complexity)

    rules = sub.add_parser("rules", help="list every lint rule with its code and severity")
    rules.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    rules.set_defaults(func=cmd_rules)

    dialects = sub.add_parser("dialects", help="show dialect differences sql-lens models")
    dialects.set_defaults(func=cmd_dialects)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE
    if getattr(args, "legacy_json", False):
        args.format = "json"
    if getattr(args, "json_flag", False):
        args.format = "json"
    try:
        return int(args.func(args))
    except UsageError as exc:
        _eprint(f"{PROG}: error: {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        _eprint(f"{PROG}: interrupted")
        return 130
    except BrokenPipeError:  # pragma: no cover - piping into head
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())