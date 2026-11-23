"""Complexity scoring tests."""

from __future__ import annotations

from sql_lens.complexity import GRADE_BANDS, grade_for, score_complexity, score_text
from sql_lens.parser import parse_one


def test_simple_query_grades_a():
    report = score_text("SELECT id FROM app.t WHERE id = 1")
    assert report.queries[0].grade == "A"
    assert report.queries[0].score < 8


def test_joins_are_counted_and_weighted():
    query = score_complexity(
        parse_one("SELECT a.id FROM a JOIN b ON a.id = b.id JOIN c ON c.a = a.id")
    )
    assert query.joins == 2
    assert query.score >= 6


def test_subquery_depth_dominates_the_score():
    shallow = score_complexity(parse_one("SELECT id FROM t"))
    deep = score_complexity(
        parse_one(
            "SELECT id FROM t WHERE id IN "
            "(SELECT id FROM u WHERE x IN (SELECT id FROM v))"
        )
    )
    assert deep.subquery_depth >= 2
    assert deep.score > shallow.score


def test_aggregates_are_counted():
    query = score_complexity(parse_one("SELECT kind, count(*), sum(x) FROM t GROUP BY kind"))
    assert query.aggregates == 2


def test_window_functions_are_counted():
    query = score_complexity(parse_one("SELECT row_number() OVER (PARTITION BY a) FROM t"))
    assert query.windows >= 1


def test_ctes_are_counted():
    query = score_complexity(parse_one("WITH a AS (SELECT 1 AS x) SELECT x FROM a"))
    assert query.ctes == 1


def test_set_operations_are_counted():
    query = score_complexity(parse_one("SELECT 1 UNION SELECT 2"))
    assert query.set_ops == 1


def test_case_branches_are_counted():
    query = score_complexity(
        parse_one("SELECT CASE WHEN a THEN 1 WHEN b THEN 2 ELSE 3 END FROM t")
    )
    assert query.cases == 2


def test_grade_bands_are_monotonic():
    for limit, grade in GRADE_BANDS:
        assert grade_for(limit) == grade
        assert grade_for(limit + 1) != grade
    assert grade_for(10_000) == "F"
    assert GRADE_BANDS[-1][1] == "D"


def test_report_piles_up_statements():
    report = score_text("SELECT 1; SELECT a FROM t JOIN u ON t.id = u.id;")
    assert len(report.queries) == 2
    assert report.total_score == sum(q.score for q in report.queries)


def test_worst_query_defines_the_file_grade():
    report = score_text(
        "SELECT 1;"
        "SELECT a.x FROM a JOIN b ON a.id = b.id JOIN c ON c.id = b.id "
        "JOIN d ON d.id = c.id WHERE a.x IN (SELECT x FROM e WHERE e.y IN "
        "(SELECT y FROM f)) GROUP BY a.x"
    )
    assert report.worst is not None
    assert report.worst.score == max(q.score for q in report.queries)
    assert report.grade == report.worst.grade


def test_breakdown_only_lists_non_zero_signals():
    query = score_complexity(parse_one("SELECT id FROM t"))
    names = [name for name, _, _ in query.breakdown()]
    assert "joins" not in names
    assert all(count > 0 for _, count, _ in query.breakdown())


def test_complexity_json_shape():
    payload = score_text("SELECT id FROM app.t").to_dict()
    assert payload["path"] == "<stdin>"
    statement = payload["statements"][0]
    assert {"score", "grade", "joins", "subquery_depth"} <= set(statement)


def test_empty_input_has_no_queries():
    report = score_text("")
    assert report.queries == []
    assert report.grade == "A"


def test_complexity_does_not_crash_on_unicode_comment():
    report = score_text("-- \u6ce8\u91ca \U0001f600\nSELECT id FROM app.t")
    assert len(report.queries) == 1
