# Roadmap and cut features

This file records what sql-lens does **not** do yet, and why. Keeping an honest
list beats shipping a feature that half-works.

## Cut from v1.0 (deliberately)

| Feature | Why it was cut | What exists instead |
| --- | --- | --- |
| `--fix` / autofix | A wrong autofix silently changes a query's meaning; the safe subset (expand `SELECT *`, add `IF NOT EXISTS`, drop a trailing comma) needs a schema-aware printer to be trustworthy | `format --write` rewrites layout only, and it refuses to write output whose token stream changed |
| Schema-aware certainty | Requires a live connection or a schema dump, which is the setup cost this tool exists to avoid | SL005/SL007/SL009 report the *pattern* with an explanation of the risk instead of asserting a missing index |
| 5 MB in under 3 s | Measured throughput is ~0.7 MB/s for lint and ~0.2 MB/s for format on a 5 MB synthetic workload; claiming the target would be a lie | Benchmark table reports the real numbers at 100 KB / 500 KB / 1 MB |
| `TOP n` parsing for SQL Server | The dialect descriptor models `TOP` and the formatter prints it, but the SELECT grammar does not consume it yet, so a T-SQL `SELECT TOP 10` is reported as a parse error | MySQL/PostgreSQL/SQLite/ANSI limit syntaxes parse correctly |
| Full SQL grammar | Supporting every vendor extension (PIVOT, MERGE, LATERAL variants, model clauses) is a different product | Unsupported statements are preserved as raw token streams for DDL rules, or rejected with a line/column |
| Jinja/dbt templating | Templated SQL is not SQL until it is rendered | If you use dbt, run sqlfluff; sql-lens is for raw migration and application SQL |

## Next

1. **Throughput.** Cache parsed statements by content hash inside one process
   (`lint`, `format` and `complexity` each re-parse today), and profile the
   formatter's verification pass. Target: 5 MB linted in under 3 s.
2. **`schema.json` mode.** Optional, offline: tables, columns, indexes and
   nullability exported from `information_schema`. Turns SL005 (`lower(email) =
   $1`), SL007 (`NOT IN` over a nullable column) and SL009 (`ORDER BY` without a
   supporting index) into certainties without ever connecting to a database.
3. **Safe `--fix`.** Only for rules where the rewrite is provably
   meaning-preserving, and only when the token signature check passes.
4. **More plan shapes.** CTE materialization choices, parallel-worker skew,
   partition-pruning failures, `Bitmap Heap Scan` recheck overhead, and
   `work_mem`-driven hash spills (SLP004 currently only covers sorts).
5. **A GitHub Action.** Annotate the exact changed lines of a PR diff, and fail
   the check only on findings introduced by that PR.
6. **More dialect coverage.** T-SQL `TOP`/`OFFSET FETCH` in the grammar, Oracle
   `ROWNUM`, and BigQuery backtick-qualified table names.
