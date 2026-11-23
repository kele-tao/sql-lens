# Changelog

All notable changes to sql-lens are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [1.0.0] - 2026-11-23

First public release.

### Added

- **Tokenizer** built on one master regular expression per dialect, recording
  line, column, quoting style and newline-before for every token. Supports
  comments (nested block comments in PostgreSQL), string literals with doubled
  quotes, PostgreSQL dollar-quoting, and placeholders (`$1`, `?`, `:name`, `%s`).
- **Recursive-descent parser** for SELECT / INSERT / UPDATE / DELETE with CTEs
  (including `RECURSIVE`), joins, derived tables, subqueries, set operations,
  window functions, `CASE`, casts and dialect-specific limit syntaxes. DDL and
  other statements are preserved as raw token streams.
- **20 lint rules** (`SL001`-`SL020`) with fixed severities, per-rule messages,
  source snippets, fix hints and per-rule reporting. Findings are sorted by
  position and are byte-for-byte deterministic.
- **Plan analyzer** for `EXPLAIN (ANALYZE, FORMAT JSON)`: five diagnosis codes
  (`SLP001`-`SLP005`) and up to three `CREATE INDEX` suggestions ranked by the
  number of rows each filter discarded.
- **Deterministic formatter** with a fixed style and a token-signature guard that
  refuses to return output whose token stream differs from the input.
- **Complexity scorer** with weights for joins, subquery depth, CTEs, predicates,
  selected columns, aggregates, set operations, window functions and CASE
  branches, mapping to an A-F grade.
- **CLI** with six commands (`lint`, `plan`, `format`, `complexity`, `rules`,
  `dialects`), four report formats (text, JSON, HTML, Markdown), documented exit
  codes 0/1/2, and `--stdin` support.
- **Bundled examples**: a report query with four real lint problems, an
  `UPDATE` without `WHERE`, a correlated N+1 subquery, a formatter reference
  file, a migration file, and a real PostgreSQL plan captured by
  `benchmarks/make_plan_fixture.py`.
- **Benchmarks** (`python benchmarks/run.py`) that generate `results.json` and
  `RESULTS.md` from a deterministic workload at 100 KB, 500 KB and 1 MB.

### Known limitations

- No `--fix` yet; every finding is report-only.
- Schema-free: nullability and index rules report patterns, not certainties.
- Lint throughput is about 0.7 MB/s; the roadmap targets 5 MB in under 3 s.
