"""CLI tests: exit codes, formats, stdin, error handling."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

from sql_lens.cli import main

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def run(argv, capsys):
    code = main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_lint_clean_file_exits_zero(tmp_path, capsys):
    path = tmp_path / "ok.sql"
    path.write_text("SELECT id FROM app.users WHERE id = 1 LIMIT 1;", encoding="utf-8")
    code, out, _ = run(["lint", str(path)], capsys)
    assert code == 0
    assert "clean" in out


def test_lint_blocker_exits_one(tmp_path, capsys):
    path = tmp_path / "bad.sql"
    path.write_text("DELETE FROM app.sessions;", encoding="utf-8")
    code, out, _ = run(["lint", str(path)], capsys)
    assert code == 1
    assert "SL002" in out
    assert "FAIL" in out


def test_lint_fail_on_warning(tmp_path, capsys):
    path = tmp_path / "warn.sql"
    path.write_text("SELECT * FROM app.users;", encoding="utf-8")
    assert run(["lint", str(path)], capsys)[0] == 0
    assert run(["lint", str(path), "--fail-on", "warning"], capsys)[0] == 1


def test_lint_json_format(tmp_path, capsys):
    path = tmp_path / "q.sql"
    path.write_text("SELECT * FROM app.users;", encoding="utf-8")
    code, out, _ = run(["lint", str(path), "--format", "json"], capsys)
    payload = json.loads(out)
    assert payload["tool"] == "sql-lens"
    assert payload["findings"][0]["rule"] == "SL001"
    assert code == 0


def test_lint_html_output_to_file(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("SELECT * FROM app.users;", encoding="utf-8")
    target = tmp_path / "report.html"
    code, out, _ = run(["lint", str(src), "--format", "html", "-o", str(target)], capsys)
    assert code == 0
    assert "wrote" in out
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_lint_markdown_format(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("SELECT * FROM app.users;", encoding="utf-8")
    code, out, _ = run(["lint", str(src), "--format", "markdown"], capsys)
    assert out.startswith("| rule |")
    assert code == 0


def test_lint_glob_and_directory(tmp_path, capsys):
    directory = tmp_path / "queries"
    directory.mkdir()
    (directory / "a.sql").write_text("SELECT * FROM app.t;", encoding="utf-8")
    (directory / "b.sql").write_text("SELECT * FROM app.u;", encoding="utf-8")
    code, out, _ = run(["lint", str(directory)], capsys)
    assert code == 0
    assert "2 files" in out
    assert "select-star" in out


def test_lint_stdin(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("DELETE FROM app.t;"))
    code = main(["lint", "--stdin"])
    out, _ = capsys.readouterr()
    assert code == 1
    assert "SL002" in out


def test_lint_missing_file_is_usage_error(tmp_path, capsys):
    code, _out, err = run(["lint", str(tmp_path / "nope.sql")], capsys)
    assert code == 2
    assert "no such file" in err


def test_lint_unknown_dialect_is_usage_error(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("SELECT 1;", encoding="utf-8")
    code, _, err = run(["lint", str(src), "--dialect", "oracle"], capsys)
    assert code == 2
    assert "unknown dialect" in err


def test_lint_bad_rule_code_exits_two(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("SELECT 1;", encoding="utf-8")
    code, _, err = run(["lint", str(src), "--select", "SL999"], capsys)
    assert code == 2
    assert "SL999" in err


def test_lint_unparseable_file_reports_parse_error_exit_one(tmp_path, capsys):
    src = tmp_path / "broken.sql"
    src.write_text("SELECT FROM WHERE (", encoding="utf-8")
    code, out, _ = run(["lint", str(src)], capsys)
    assert code == 1
    assert "parse error" in out


def test_plan_example_runs(capsys):
    code, out, _ = run(["plan", str(EXAMPLES / "explain_orders.json")], capsys)
    assert code == 0
    assert "sql-lens plan" in out
    assert "CREATE INDEX ON" in out


def test_plan_json_format(capsys):
    code, out, _ = run(
        ["plan", str(EXAMPLES / "explain_orders.json"), "--format", "json"], capsys
    )
    payload = json.loads(out)
    assert payload["summary"]["findings"] >= 1
    assert payload["suggested_indexes"]
    assert code == 0


def test_plan_fail_on_any(tmp_path, capsys):
    code, _, _ = run(
        ["plan", str(EXAMPLES / "explain_orders.json"), "--fail-on-any"], capsys
    )
    assert code == 1


def test_plan_bad_file_is_usage_error(tmp_path, capsys):
    bad = tmp_path / "plan.json"
    bad.write_text("{not json", encoding="utf-8")
    code, _, err = run(["plan", str(bad)], capsys)
    assert code == 2
    assert "invalid JSON" in err


def test_format_stdout(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("select id, name from t", encoding="utf-8")
    code, out, _ = run(["format", str(src)], capsys)
    assert code == 0
    assert out.startswith("SELECT")


def test_format_check_exits_one_when_unformatted(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("select id from t", encoding="utf-8")
    code, _, err = run(["format", str(src), "--check"], capsys)
    assert code == 1
    assert "would reformat" in err


def test_format_check_passes_when_already_formatted(tmp_path, capsys):
    from sql_lens.formatter import format_sql

    src = tmp_path / "q.sql"
    src.write_text(format_sql("select id from t"), encoding="utf-8")
    code, out, _ = run(["format", str(src), "--check"], capsys)
    assert code == 0
    assert "already formatted" in out


def test_format_write_rewrites_file(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("select id, name from t", encoding="utf-8")
    code, _, _ = run(["format", str(src), "--write"], capsys)
    assert code == 0
    assert src.read_text(encoding="utf-8").startswith("SELECT")


def test_format_unparseable_sql_is_usage_error(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("select from (", encoding="utf-8")
    code, _, err = run(["format", str(src)], capsys)
    assert code == 2
    assert "does not parse" in err


def test_complexity_text(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("SELECT a FROM t JOIN u ON t.id = u.id", encoding="utf-8")
    code, out, _ = run(["complexity", str(src)], capsys)
    assert code == 0
    assert "grade" in out.lower()


def test_complexity_json(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text("SELECT a FROM t", encoding="utf-8")
    _code, out, _ = run(["complexity", str(src), "--format", "json"], capsys)
    payload = json.loads(out)
    assert payload["reports"][0]["statements"][0]["grade"] == "A"


def test_complexity_max_grade_gate(tmp_path, capsys):
    src = tmp_path / "q.sql"
    src.write_text(
        "SELECT a.x FROM a JOIN b ON a.id = b.id JOIN c ON c.id = b.id "
        "JOIN d ON d.id = c.id JOIN e ON e.id = d.id JOIN f ON f.id = e.id",
        encoding="utf-8",
    )
    assert run(["complexity", str(src), "--max-grade", "A"], capsys)[0] == 1
    assert run(["complexity", str(src), "--max-grade", "F"], capsys)[0] == 0


def test_rules_command_lists_twenty(capsys):
    code, out, _ = run(["rules"], capsys)
    assert code == 0
    assert out.count("SL0") >= 20


def test_rules_markdown(capsys):
    code, out, _ = run(["rules", "--format", "markdown"], capsys)
    assert code == 0
    assert out.startswith("| code |")


def test_rules_json(capsys):
    _code, out, _ = run(["rules", "--format", "json"], capsys)
    payload = json.loads(out)
    assert len(payload) == 20
    assert payload[0]["code"] == "SL001"


def test_dialects_command(capsys):
    code, out, _ = run(["dialects"], capsys)
    assert code == 0
    for name in ("postgres", "mysql", "sqlite", "sqlserver", "ansi"):
        assert name in out


def test_no_command_prints_help_and_returns_two(capsys):
    code, out, _ = run([], capsys)
    assert code == 2
    assert "usage" in out.lower()


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "1.0.0" in capsys.readouterr().out


def test_module_entrypoint_exists():
    import sql_lens.__main__ as entry

    assert entry.main is main