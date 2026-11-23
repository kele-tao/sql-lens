# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 1.x | yes |

## What sql-lens does with your SQL

sql-lens is a static analyser. It reads files you point it at, or standard
input, and writes a report. Specifically:

- It **never opens a network connection.** There is no telemetry, no update
  check and no remote rule source. Tests run fully offline.
- It **never connects to a database.** The plan analyzer works on a JSON file
  you export yourself with `EXPLAIN (ANALYZE, FORMAT JSON)`.
- It **never executes SQL.** The parser builds an AST; nothing is evaluated.
- It **only reads** files passed on the command line (plus directories/globs
  you name). `format --write` is the only command that writes, and it only
  rewrites the files you passed it.
- It **only writes** to the `--output` path you provide, if any.

Query text, table names and literals can be sensitive. Findings include the
offending source line in the terminal, JSON and HTML reports - treat a report as
being as sensitive as the SQL it came from.

## Hardening notes

- Input files are capped at 8 MB so a hostile or malformed file cannot exhaust
  memory; larger files are rejected with a usage error.
- The parser is recursive descent with explicit nesting; deeply nested input
  produces a parse error rather than unbounded recursion in the normal case.
- Reports written with `--format html` escape all finding text before embedding
  it in the document.

## Reporting a vulnerability

Please open a private security advisory on GitHub
(https://github.com/kele-tao/sql-lens/security/advisories/new) rather than a
public issue. Include the command, a minimal input, and the impact you believe
it has. We aim to respond within a week.
