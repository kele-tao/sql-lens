"""Terminal, HTML and Markdown rendering for lint and plan reports.

The terminal renderer is the default output: colour only when stdout is a TTY
or FORCE_COLOR is set, so piped output stays diffable and CI logs stay readable.
The HTML renderer emits a single self-contained file with no external assets.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from .model import Finding, Severity

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
GREY = "\x1b[90m"

BT = chr(96)

SEVERITY_COLOUR = {
    Severity.ERROR: RED,
    Severity.WARNING: YELLOW,
    Severity.INFO: BLUE,
}

SEVERITY_LABEL = {
    Severity.ERROR: "error",
    Severity.WARNING: "warn",
    Severity.INFO: "info",
}

GRADE_COLOUR = {"A": GREEN, "B": CYAN, "C": YELLOW, "D": YELLOW, "F": RED}


def colour_enabled(stream=None) -> bool:
    """True when ANSI colour should be written to *stream*."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


class Palette:
    """Tiny helper so renderers never concatenate raw escapes by hand."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def paint(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return "".join(codes) + text + RESET

    def bold(self, text: str) -> str:
        return self.paint(text, BOLD)

    def dim(self, text: str) -> str:
        return self.paint(text, DIM)

    def severity(self, severity: Severity) -> str:
        return self.paint(SEVERITY_LABEL[severity].upper().ljust(5), SEVERITY_COLOUR[severity], BOLD)

    def grade(self, grade: str) -> str:
        return self.paint(grade, GRADE_COLOUR.get(grade, GREY), BOLD)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


def _rule_table(findings: Sequence[Finding], palette: Palette) -> list[str]:
    """A per-rule summary: code, count, severity, rule name."""
    counts: dict[str, list[Finding]] = {}
    for f in findings:
        counts.setdefault(f.rule, []).append(f)
    lines = [palette.bold("  by rule")]
    lines.append(palette.dim("  code   count  severity  rule"))
    for code in sorted(counts):
        group = counts[code]
        first = group[0]
        label = SEVERITY_LABEL[first.severity].ljust(8)
        lines.append(
            f"  {palette.paint(code, BOLD, CYAN):<7}{len(group):<7}{palette.paint(label, SEVERITY_COLOUR[first.severity])} {first.title}"
        )
    return lines


def render_text(report, color: bool | None = None) -> str:
    """Render a LintReport for a terminal."""
    if color is None:
        color = colour_enabled()
    p = Palette(color)
    out: list[str] = []
    files = report.files or ["<stdin>"]
    lines_scanned = report.lines_scanned
    out.append(
        p.bold(
            f"sql-lens lint  {len(files)} {_plural(len(files), 'file')}, "
            f"{lines_scanned} {_plural(lines_scanned, 'line')}"
        )
    )
    for err in report.parse_errors:
        out.append(
            p.paint("  parse error  ", RED, BOLD)
            + f"{err['path']}:{err['line']}:{err['column']}  {err['message']}"
        )

    if not report.findings:
        out.append("")
        out.append(p.paint("  clean: no findings", GREEN, BOLD))
        return "\n".join(out) + "\n"

    current_file = None
    for f in report.findings:
        if f.path != current_file:
            current_file = f.path
            out.append("")
            out.append(p.bold(f"  {f.path}"))
        location = f"{f.line}:{f.col}"
        out.append(
            f"  {p.paint(location.ljust(7), GREY)} {p.severity(f.severity)} "
            f"{p.paint(f.rule, BOLD, MAGENTA)}  {f.message}"
        )
        if f.snippet:
            out.append(f"          {p.dim(f.snippet)}")
        if f.hint:
            out.append(f"          {p.paint('hint: ' + f.hint, CYAN)}")

    out.append("")
    out.extend(_rule_table(report.findings, p))
    out.append("")
    errors = report.count(Severity.ERROR)
    warnings = report.count(Severity.WARNING)
    infos = report.count(Severity.INFO)
    summary = (
        f"{errors} {_plural(errors, 'error')}, "
        f"{warnings} {_plural(warnings, 'warning')}, "
        f"{infos} info"
    )
    verdict = p.paint("FAIL", RED, BOLD) if report.failed() else p.paint("PASS", GREEN, BOLD)
    out.append(f"  {p.bold('result')}  {verdict}  {summary}  (fail-on {report.fail_on.value})")
    return "\n".join(out) + "\n"


def render_plan_text(analysis, color: bool | None = None) -> str:
    """Render a PlanAnalysis for a terminal."""
    if color is None:
        color = colour_enabled()
    p = Palette(color)
    out: list[str] = []
    out.append(
        p.bold(
            f"sql-lens plan  {analysis.node_count} plan "
            f"{_plural(analysis.node_count, 'node')}  "
            f"exec {analysis.execution_time:.2f} ms  plan {analysis.planning_time:.2f} ms"
        )
    )
    if not analysis.findings:
        out.append("")
        out.append(p.paint("  clean: no plan problems detected", GREEN, BOLD))
    for f in analysis.findings:
        out.append("")
        out.append(f"  {p.severity(f.severity)} {p.paint(f.code, BOLD, MAGENTA)}  {f.title}")
        out.append(f"          {f.message}")
        if f.hint:
            out.append(f"          {p.paint('hint: ' + f.hint, CYAN)}")
    if analysis.suggestions:
        out.append("")
        out.append(p.bold("  suggested indexes"))
        for i, s in enumerate(analysis.suggestions, start=1):
            out.append(f"  {i}. {p.paint(s.ddl, GREEN, BOLD)}")
            out.append(f"     {p.dim(s.reason)}")
    out.append("")
    errors = analysis.count(Severity.ERROR)
    warnings = analysis.count(Severity.WARNING)
    verdict = p.paint("FAIL", RED, BOLD) if analysis.has_blockers() else p.paint("PASS", GREEN, BOLD)
    out.append(
        f"  {p.bold('result')}  {verdict}  {len(analysis.findings)} "
        f"{_plural(len(analysis.findings), 'finding')} ({errors} errors, {warnings} warnings)"
    )
    return "\n".join(out) + "\n"


def render_complexity_text(report, color: bool | None = None) -> str:
    """Render a ComplexityReport for a terminal."""
    if color is None:
        color = colour_enabled()
    p = Palette(color)
    out: list[str] = [p.bold(f"sql-lens complexity  {report.path}")]
    if not report.queries:
        out.append("")
        out.append(p.dim("  no statements found"))
        return "\n".join(out) + "\n"
    for query in report.queries:
        out.append("")
        out.append(
            f"  {p.bold(query.label)}  score {p.bold(str(query.score))}  "
            f"grade {p.grade(query.grade)}"
        )
        rows = query.breakdown()
        if not rows:
            out.append(f"    {p.dim('no complexity signals')}")
            continue
        width = max(len(name) for name, _, _ in rows)
        for name, count, points in rows:
            out.append(f"    {name.ljust(width)}  {count:>3} x  = {points:>3}")
    out.append("")
    total = report.total_score
    out.append(
        f"  {p.bold('worst grade')}  {p.grade(report.grade)}  "
        f"{p.dim(f'total score {total} across {len(report.queries)} statements')}"
    )
    return "\n".join(out) + "\n"


HTML_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px; background: #0b0f17; color: #d7dee9;
       font: 15px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
h1 span { color: #22d3ee; }
.meta { color: #7d8b9f; font-size: 13px; margin-bottom: 26px; }
table { border-collapse: collapse; width: 100%; margin: 0 0 26px; font-size: 14px; }
th { text-align: left; color: #8b9bb4; font-weight: 600; padding: 8px 10px;
     border-bottom: 1px solid #263042; }
td { padding: 8px 10px; border-bottom: 1px solid #17202d; vertical-align: top; }
tr:hover td { background: #111826; }
code { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 13px; }
.sev { display: inline-block; min-width: 58px; text-align: center; padding: 1px 8px;
       border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: .04em; }
.error { background: #3b1417; color: #ff8787; }
.warn  { background: #3a2c0b; color: #fbbf24; }
.info  { background: #10243a; color: #93c5fd; }
.code  { color: #c084fc; font-weight: 700; }
.snip  { color: #7d8b9f; font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.hint  { color: #67e8f9; font-size: 12px; }
.summary { display: flex; gap: 18px; margin: 0 0 26px; }
.card { background: #111826; border: 1px solid #1f2937; border-radius: 12px; padding: 14px 20px; }
.card b { display: block; font-size: 24px; }
.card span { color: #8b9bb4; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
.pass { color: #4ade80; } .fail { color: #ff8787; }
"""


def render_html(report) -> str:
    """Render a LintReport as one self-contained HTML document."""
    esc = __import__("html").escape
    files = report.files or ["<stdin>"]
    sev_counts = {s: report.count(s) for s in Severity}
    verdict = "FAIL" if report.failed() else "PASS"
    rows: list[str] = []
    for f in report.findings:
        rows.append(
            "<tr>"
            f'<td class="code">{esc(f.rule)}</td>'
            f'<td><span class="sev {f.severity.value}">{f.severity.value.upper()}</span></td>'
            f"<td>{esc(f.path)}:{f.line}:{f.col}</td>"
            f"<td>{esc(f.message)}"
            + (f'<div class="snip">{esc(f.snippet)}</div>' if f.snippet else "")
            + (f'<div class="hint">hint: {esc(f.hint)}</div>' if f.hint else "")
            + "</td></tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4" class="pass">clean: no findings</td></tr>')
    joined = "\n".join(rows)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>sql-lens report</title><style>{HTML_STYLE}</style></head>
<body>
<h1><span>sql-lens</span> lint report</h1>
<div class="meta">{esc(", ".join(files))} &middot; {report.lines_scanned} lines scanned &middot;
{len(report.rules_enabled)} rules enabled &middot; fail-on {esc(report.fail_on.value)}</div>
<div class="summary">
  <div class="card"><b class="{'fail' if verdict == 'FAIL' else 'pass'}">{verdict}</b><span>result</span></div>
  <div class="card"><b class="fail">{sev_counts[Severity.ERROR]}</b><span>errors</span></div>
  <div class="card"><b class="warn">{sev_counts[Severity.WARNING]}</b><span>warnings</span></div>
  <div class="card"><b class="info">{sev_counts[Severity.INFO]}</b><span>info</span></div>
</div>
<table>
<thead><tr><th>rule</th><th>severity</th><th>location</th><th>finding</th></tr></thead>
<tbody>
{joined}
</tbody>
</table>
</body></html>
"""


def render_markdown(report) -> str:
    """Render a LintReport as a Markdown table (for PR comments)."""
    lines = [
        "| rule | severity | location | finding |",
        "| --- | --- | --- | --- |",
    ]
    for f in report.findings:
        message = f.message.replace("|", "\\|")
        rule = BT + f.rule + BT
        where = BT + f"{f.path}:{f.line}:{f.col}" + BT
        lines.append(f"| {rule} | {f.severity.value} | {where} | {message} |")
    if not report.findings:
        lines.append("| - | - | - | clean: no findings |")
    return "\n".join(lines) + "\n"


__all__ = [
    "Palette",
    "colour_enabled",
    "render_complexity_text",
    "render_html",
    "render_markdown",
    "render_plan_text",
    "render_text",
]