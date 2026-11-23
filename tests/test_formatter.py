"""Formatter tests: style, determinism and the never-corrupt guarantee."""

from __future__ import annotations

import pathlib

import pytest

from sql_lens.dialects import DIALECTS
from sql_lens.formatter import format_sql

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"


def test_keywords_are_uppercased():
    assert format_sql("select id from t").startswith("SELECT id")


def test_trailing_commas_are_used_between_columns():
    out = format_sql("select a, b, c from t")
    assert "a," in out and "b," in out


def test_one_clause_per_line():
    out = format_sql("select id from t where x = 1 order by id limit 5")
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].startswith("SELECT")
    assert any(line.startswith("FROM ") for line in lines)
    assert any(line.startswith("WHERE ") for line in lines)
    assert any(line.startswith("ORDER BY ") for line in lines)
    assert any(line.startswith("LIMIT ") for line in lines)


def test_join_gets_its_own_line():
    out = format_sql("select a.id from a join b on b.id = a.id where a.x = 1")
    assert "JOIN b" in out
    assert "\nWHERE a.x = 1" in out


def test_subquery_is_indented():
    out = format_sql("select id from (select id from t) as s")
    assert "(\n" in out, out
    assert "  SELECT id" in out


def test_idempotent_on_generated_sql():
    sql = (
        "select c.id, count(*) as n from app.customers c "
        "join app.orders o on o.customer_id = c.id "
        "where c.country in ('DE', 'US') group by c.id having count(*) > 1 "
        "order by n desc limit 20"
    )
    once = format_sql(sql)
    assert format_sql(once) == once


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.sql")), ids=lambda p: p.name)
def test_every_example_is_idempotent_and_still_parses(path):
    from sql_lens.parser import parse_all

    sql = path.read_text(encoding="utf-8")
    once = format_sql(sql)
    assert format_sql(once) == once
    assert parse_all(once, "postgres")


def test_semicolon_is_kept():
    assert format_sql("select 1;").rstrip().endswith(";")


def test_multiple_statements_are_separated_by_a_blank_line():
    out = format_sql("select 1; select 2;")
    assert out.count("SELECT") == 2
    assert "\n\n" in out


def test_empty_input_formats_to_empty_string():
    assert format_sql("") == ""
    assert format_sql("   \n\t") == ""


def test_comments_are_preserved():
    out = format_sql("select id -- the key\nfrom t")
    assert "-- the key" in out


def test_dialect_quoting_is_respected():
    out = format_sql("select [order] from t", "sqlserver")
    assert "[order]" in out
    assert format_sql("select `x` from t", "mysql").strip().startswith("SELECT")


def test_verified_formatting_detects_a_bug(monkeypatch):
    import sql_lens.formatter as formatter_module

    monkeypatch.setattr(formatter_module, "_signature", lambda sql, dia: ["token", sql])
    with pytest.raises(ValueError) as excinfo:
        format_sql("select id from t")
    assert "refusing to rewrite it" in str(excinfo.value)


def test_every_dialect_can_format_a_simple_query():
    for name in DIALECTS:
        assert "SELECT id" in format_sql("select id from t where a = 1", name)
def test_ddl_statements_keep_their_original_text():
    # DDL is not modelled by the formatter, so it is passed through untouched
    # apart from trailing whitespace.
    sql = "CREATE TABLE app.t (id int);"
    assert format_sql(sql).strip() == sql


def test_formatter_output_reparses_as_the_same_tokens():
    from sql_lens.dialects import get_dialect
    from sql_lens.formatter import _signature

    sql = "update accounts set locked = true, note = 'x' where id = 1"
    dia = get_dialect("postgres")
    assert _signature(sql, dia) == _signature(format_sql(sql), dia)


def test_tokens_carry_source_offsets():
    from sql_lens.lexer import tokenize

    sql = "SELECT id FROM t"
    tokens = tokenize(sql)
    assert sql[tokens[0].start : tokens[0].end] == "SELECT"
    assert sql[tokens[3].start : tokens[3].end] == "t"