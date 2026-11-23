# Contributing to sql-lens

Thanks for taking the time to contribute. This project is small on purpose, and
the strongest contributions are narrow and testable.

## Development setup

```bash
git clone https://github.com/kele-tao/sql-lens
cd sql-lens
python -m pip install -e ".[dev]"
python -m pytest -q        # 195 tests, offline, about one second
python -m ruff check .     # must be clean
```

sql-lens itself has **no runtime dependencies**. Please keep it that way: if a
change can be written against the standard library, it must be.

## The two kinds of contribution

### A new lint rule

1. Add a `Rule` entry to `RULES` in `src/sql_lens/lint.py`. Codes are assigned in
   order (`SL021` next) and **never change meaning** afterwards.
2. Write the rule as a generator function next to the existing ones:
   `def rule_x(ctx: FileContext, select: Select) -> Iterator[Finding]`. Rules may
   not touch the filesystem, open connections or call each other.
3. Wire it into the driver in `lint_file` and add tests to `tests/test_lint.py`
   that assert both directions: the pattern is reported, and the corrected query
   is not.
4. If the rule needs schema knowledge to be certain, make it a *pattern* rule
   and say so in the rule's `summary` and `hint`.

### A parser gap

Reduce the failure to the smallest query that reproduces it, add it to
`tests/test_parser.py`, then change the grammar. A construct that cannot be
supported must raise `ParseError` with a line and column - never be silently
skipped or reinterpreted.

## Ground rules

- **No faked output.** Every number, table and screenshot in the README must be
  produced by a command a stranger can run.
- **Determinism.** Same input, same output, byte for byte. If you introduce
  randomness, it must be seeded and exposed via `--seed`.
- **No new runtime dependencies.** `pytest` and `ruff` are the only dev extras.
- **Errors are for humans.** A user mistake is a one-line message plus exit code
  2, never a traceback.
- **Style.** `ruff check .` clean, line length 100, type hints on public
  functions.

## Reporting a bug

Open an issue with the exact command, the SQL that triggers it (redact anything
confidential) and the output you got. A plan-analysis bug is easiest to fix when
you attach the `EXPLAIN (ANALYZE, FORMAT JSON)` document.

## Pull request checklist

- [ ] `python -m pytest -q` passes
- [ ] `python -m ruff check .` is clean
- [ ] New behaviour has a test that would fail without the change
- [ ] If you touched the hot path, `python benchmarks/run.py` was re-run and the
      README table updated with the new numbers
- [ ] `CHANGELOG.md` has an entry under "Unreleased"
