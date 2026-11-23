"""Lint rule tests: one behaviour per rule, plus driver behaviour."""

from __future__ import annotations

import pytest

from sql_lens.lint import RULES, LintOptions, lint_sources, lint_text, resolve_rules
from sql_lens.model import Severity


def codes(sql, dialect="postgres", **kwargs):
    report = lint_text(sql, "query.sql", LintOptions(dialect=dialect, **kwargs))
    return {f.rule for f in report.findings}


def test_every_rule_has_a_unique_code_and_example():
    assert len(RULES) == 20
    assert len({r.code for r in RULES}) == 20
    assert [r.code for r in RULES] == [f"SL{index:03d}" for index in range(1, 21)]
    for rule in RULES:
        assert rule.summary and rule.hint and rule.example
        assert rule.severity in set(Severity)


def test_sl001_select_star():
    assert "SL001" in codes("SELECT * FROM app.users")


def test_sl002_missing_where_is_an_error_and_fails_the_gate():
    report = lint_text("DELETE FROM app.sessions")
    assert [f.rule for f in report.findings] == ["SL002"]
    assert report.findings[0].severity is Severity.ERROR
    assert report.failed() is True


def test_sl003_unbounded_dml():
    assert "SL003" in codes("UPDATE app.accounts SET locked = true WHERE id > 0")


def test_sl004_implicit_cross_join():
    assert "SL004" in codes("SELECT a.id FROM app.a JOIN app.b")


def test_sl005_non_sargable_predicate():
    assert "SL005" in codes("SELECT id FROM app.users WHERE lower(email) = 'x'")


def test_sl005_not_triggered_for_plain_column():
    assert "SL005" not in codes("SELECT id FROM app.users WHERE email = 'x'")


def test_sl006_leading_wildcard():
    assert "SL006" in codes("SELECT id FROM app.users WHERE name LIKE '%son'")


def test_sl006_silent_for_prefix_search():
    assert "SL006" not in codes("SELECT id FROM app.users WHERE name LIKE 'son%'")


def test_sl007_not_in():
    assert "SL007" in codes("SELECT id FROM app.t WHERE id NOT IN (SELECT id FROM app.b)")


def test_sl008_or_in_join():
    assert "SL008" in codes(
        "SELECT 1 FROM app.a JOIN app.b ON a.x = b.x OR a.y = b.y"
    )


def test_sl009_order_by_without_limit():
    assert "SL009" in codes("SELECT id FROM app.t ORDER BY id")


def test_sl009_silent_with_limit():
    assert "SL009" not in codes("SELECT id FROM app.t ORDER BY id LIMIT 10")


def test_sl010_cartesian_product_from_comma_join():
    assert "SL010" in codes(
        "SELECT a.id FROM app.a, app.b WHERE a.total > 5"
    )


def test_sl010_silent_when_predicate_connects_both_tables():
    assert "SL010" not in codes(
        "SELECT a.id FROM app.a, app.b WHERE a.id = b.a_id"
    )


def test_sl011_redundant_distinct_with_group_by():
    assert "SL011" in codes("SELECT DISTINCT count(*) FROM app.t GROUP BY kind")


def test_sl012_unused_cte():
    assert "SL012" in codes("WITH stale AS (SELECT 1) SELECT id FROM app.t")


def test_sl012_silent_when_cte_used():
    assert "SL012" not in codes("WITH recent AS (SELECT 1 AS id) SELECT id FROM recent")


def test_sl013_correlated_subquery():
    assert "SL013" in codes(
        "SELECT o.id FROM app.orders o "
        "WHERE EXISTS (SELECT 1 FROM app.items i WHERE i.order_id = o.id)"
    )


def test_sl014_hardcoded_literals_in_update():
    assert "SL014" in codes("UPDATE app.users SET role = 'admin' WHERE id = 4")


def test_sl015_ddl_without_if_exists():
    assert "SL015" in codes("DROP TABLE app.old_events;")


def test_sl015_silent_with_if_exists():
    assert "SL015" not in codes("DROP TABLE IF EXISTS app.old_events;")


def test_sl016_unsafe_column_type_change():
    assert "SL016" in codes("ALTER TABLE app.t ALTER COLUMN id TYPE bigint;")


def test_sl017_missing_schema_prefix():
    assert "SL017" in codes("SELECT id FROM users")


def test_sl017_silent_with_schema():
    assert "SL017" not in codes("SELECT id FROM app.users")


def test_sl018_reserved_word_identifier():
    assert "SL018" in codes("SELECT order, group FROM app.sales")


def test_sl019_mixed_case_keywords():
    assert "SL019" in codes("select id FROM app.users WHERE id = 1")


def test_sl020_trailing_comma_before_clause():
    report = lint_text("SELECT id, FROM app.users")
    assert report.findings
    assert report.findings[0].rule == "SL020"


def test_select_and_ignore_filters():
    options = LintOptions(select=("SL001",))
    report = lint_text("SELECT * FROM users", "q.sql", options)
    assert {f.rule for f in report.findings} == {"SL001"}
    options = LintOptions(ignore=("SL001", "SL017"))
    report = lint_text("SELECT * FROM users", "q.sql", options)
    assert report.findings == []


def test_unknown_rule_code_is_a_clear_error():
    with pytest.raises(ValueError) as excinfo:
        resolve_rules(LintOptions(select=("SL999",)))
    assert "SL999" in str(excinfo.value)
    assert "known codes" in str(excinfo.value)


def test_min_severity_filters_info():
    options = LintOptions(min_severity=Severity.WARNING)
    report = lint_text("SELECT id FROM users", "q.sql", options)
    assert all(f.severity.rank >= 2 for f in report.findings)
    assert "SL017" not in {f.rule for f in report.findings}


def test_fail_on_warning():
    options = LintOptions(fail_on=Severity.WARNING)
    report = lint_text("SELECT * FROM app.users", "q.sql", options)
    assert report.failed() is True


def test_clean_query_reports_no_findings():
    report = lint_text(
        "SELECT id FROM app.users WHERE id = 1 ORDER BY id LIMIT 1", "q.sql"
    )
    assert report.findings == []
    assert report.failed() is False


def test_parse_error_is_reported_not_raised():
    report = lint_text("SELECT FROM WHERE (", "broken.sql")
    assert report.parse_errors
    assert report.parse_errors[0]["line"] >= 1
    assert report.failed() is True


def test_lex_error_is_reported_not_raised():
    report = lint_text("SELECT 'unterminated", "broken.sql")
    assert report.parse_errors
    assert "unterminated" in report.parse_errors[0]["message"]


def test_findings_are_sorted_by_position():
    report = lint_text("SELECT * FROM users ORDER BY id", "q.sql")
    positions = [(f.line, f.col) for f in report.findings]
    assert positions == sorted(positions)


def test_findings_carry_snippet_and_path():
    report = lint_text("SELECT *\nFROM app.users", "query.sql")
    first = report.findings[0]
    assert first.path == "query.sql"
    assert first.snippet == "SELECT *"


def test_json_report_shape():
    payload = lint_text("SELECT * FROM users", "q.sql").to_dict()
    assert payload["tool"] == "sql-lens"
    assert payload["summary"]["total"] == payload["summary"]["warnings"] + payload["summary"][
        "errors"
    ] + payload["summary"]["info"]
    assert payload["by_rule"]
    assert isinstance(payload["findings"][0]["line"], int)


def test_html_report_is_self_contained():
    html = lint_text("SELECT * FROM users", "q.sql").to_html()
    assert html.startswith("<!doctype html>")
    assert "<style>" in html
    assert "SL001" in html
    assert "http://" not in html and "https://" not in html


def test_markdown_report_has_a_table():
    md = lint_text("SELECT * FROM users", "q.sql").to_markdown()
    assert md.splitlines()[0].startswith("| rule |")
    assert "SL001" in md


def test_lint_sources_combines_files_and_counts_lines():
    report = lint_sources(
        [("a.sql", "SELECT * FROM t"), ("b.sql", "SELECT * FROM u\n")],
        LintOptions(select=("SL001",)),
    )
    assert report.files == ["a.sql", "b.sql"]
    assert len(report.findings) == 2
    assert report.lines_scanned >= 2


def test_unicode_sql_lints_without_crashing():
    sql = "SELECT \u7528\u6237\u540d FROM \u7528\u6237 WHERE \ud83d\ude00 = 'ok'"
    report = lint_text(sql, "unicode.sql")
    assert isinstance(report.findings, list)


def test_large_input_smoke_test():
    sql = "SELECT id FROM app.t WHERE id = 1;\n" * 2000
    report = lint_text(sql, "big.sql")
    assert report.lines_scanned == 2000
    assert report.findings == []