"""Plan analysis tests, driven by a real EXPLAIN (ANALYZE, FORMAT JSON) fixture."""

from __future__ import annotations

import json
import pathlib

import pytest

from sql_lens.model import Severity
from sql_lens.plan import (
    PlanError,
    analyse_plan,
    extract_columns,
    load_plan,
    walk_plan,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "explain_orders.json"


@pytest.fixture(scope="module")
def plan_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def analysis(plan_text):
    return analyse_plan(plan_text, path=str(FIXTURE))


def test_fixture_is_a_real_postgres_plan():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    root = payload[0]["Plan"]
    assert "Node Type" in root
    assert payload[0]["Execution Time"] > 0
    assert root["Actual Loops"] == 1


def test_analysis_reads_timings_and_node_count(analysis):
    assert analysis.execution_time > 0
    assert analysis.planning_time > 0
    assert analysis.node_count >= 4


def test_sequential_scan_over_many_rows_is_reported(analysis):
    codes = [f.code for f in analysis.findings]
    assert "SLP001" in codes
    seq = [f for f in analysis.findings if f.code == "SLP001"]
    assert all("rows" in f.message for f in seq)


def test_filter_discarding_rows_is_reported(analysis):
    assert any(f.code == "SLP005" for f in analysis.findings)


def test_index_suggestions_are_derived_from_real_predicates(analysis):
    assert 1 <= len(analysis.suggestions) <= 3
    for suggestion in analysis.suggestions:
        assert suggestion.ddl.startswith("CREATE INDEX ON ")
        assert suggestion.columns
        assert suggestion.ddl.endswith(");")
        assert suggestion.rows_discarded >= 0
    tables = {s.table for s in analysis.suggestions}
    assert tables <= {"orders", "customers"}


def test_suggestions_are_ranked_by_rows_discarded(analysis):
    discarded = [s.rows_discarded for s in analysis.suggestions]
    assert discarded == sorted(discarded, reverse=True)


def test_findings_are_sorted_most_severe_first(analysis):
    ranks = [f.severity.rank for f in analysis.findings]
    assert ranks == sorted(ranks, reverse=True)


def test_plan_json_shape(analysis):
    payload = analysis.to_dict()
    assert payload["tool"] == "sql-lens"
    assert payload["summary"]["findings"] == len(analysis.findings)
    assert payload["suggested_indexes"][0]["ddl"].startswith("CREATE INDEX")


def test_walk_plan_visits_children(analysis):
    nodes = list(walk_plan(analysis.root))
    assert len(nodes) == analysis.node_count
    assert any(node.children for node in nodes)


def test_large_rows_threshold_changes_findings(plan_text):
    strict = analyse_plan(plan_text, large_rows=100_000_000)
    assert not any(f.code in {"SLP001", "SLP005"} for f in strict.findings)


def test_estimate_mismatch_is_reported_when_present(plan_text):
    synthetic = {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "events",
            "Plan Rows": 1,
            "Actual Rows": 5000,
            "Actual Loops": 1,
            "Total Cost": 100.0,
            "Filter": "(tenant_id = 42)",
            "Rows Removed by Filter": 10,
        },
        "Planning Time": 0.3,
        "Execution Time": 12.0,
    }
    result = analyse_plan(json.dumps([synthetic]))
    codes = {f.code for f in result.findings}
    assert "SLP003" in codes
    message = next(f.message for f in result.findings if f.code == "SLP003")
    assert "5,000x" in message


def test_nested_loop_with_many_rows_is_an_error():
    plan = {
        "Plan": {
            "Node Type": "Nested Loop",
            "Join Type": "Inner",
            "Plan Rows": 20000,
            "Actual Rows": 25000,
            "Actual Loops": 1,
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "a", "Plan Rows": 100,
                 "Actual Rows": 100, "Actual Loops": 1},
                {"Node Type": "Index Scan", "Relation Name": "b", "Plan Rows": 250,
                 "Actual Rows": 250, "Actual Loops": 1},
            ],
        }
    }
    result = analyse_plan(json.dumps([plan]))
    assert any(f.code == "SLP002" and f.severity is Severity.ERROR for f in result.findings)
    assert result.has_blockers() is True


def test_sort_spill_to_disk_is_reported():
    plan = {
        "Plan": {
            "Node Type": "Sort",
            "Plan Rows": 1000,
            "Actual Rows": 1000,
            "Actual Loops": 1,
            "Sort Method": "external merge",
            "Sort Space Used": 20480,
            "Sort Key": ["created_at"],
        }
    }
    result = analyse_plan(json.dumps([plan]))
    finding = next(f for f in result.findings if f.code == "SLP004")
    assert "20,480 kB" in finding.message


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not json at all",
        '{"hello": "world"}',
        "[]",
    ],
)
def test_bad_input_raises_a_helpful_plan_error(bad):
    with pytest.raises(PlanError):
        analyse_plan(bad)


def test_plan_error_mentions_explain_syntax():
    with pytest.raises(PlanError) as excinfo:
        analyse_plan('{"a": 1}')
    assert "EXPLAIN" in str(excinfo.value)


def test_invalid_json_reports_line_and_column():
    with pytest.raises(PlanError) as excinfo:
        analyse_plan('{\n  "Plan": oops\n}')
    assert "line 2" in str(excinfo.value)


def test_negative_large_rows_is_rejected(plan_text):
    with pytest.raises(ValueError):
        analyse_plan(plan_text, large_rows=0)


def test_extract_columns_finds_predicate_columns():
    assert extract_columns("(status = 'paid'::text)") == ["status"]
    assert "email" in extract_columns("(lower(email) = 'a@b.c'::text)")
    assert extract_columns("") == []


def test_extract_columns_ignores_sql_words():
    columns = extract_columns("(deleted_at IS NULL)")
    assert "null" not in [c.lower() for c in columns]


def test_load_plan_accepts_a_bare_plan_object():
    root, doc = load_plan('{"Node Type": "Seq Scan", "Relation Name": "t"}')
    assert root.node_type == "Seq Scan"
    # a bare node has no timing wrapper, so the document carries no timings
    assert "Execution Time" not in doc


def test_load_plan_accepts_a_wrapped_object():
    root, doc = load_plan('{"Plan": {"Node Type": "Result"}}')
    assert root.node_type == "Result"
    assert "Plan" in doc


def test_synthetic_plan_with_no_issues_is_clean():
    plan = {
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "t",
            "Plan Rows": 1,
            "Actual Rows": 1,
            "Actual Loops": 1,
            "Index Cond": "(id = 1)",
        }
    }
    result = analyse_plan(json.dumps([plan]))
    assert result.findings == []
    assert result.has_blockers() is False