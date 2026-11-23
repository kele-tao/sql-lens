"""Parser tests: shapes, dialects, error messages."""

from __future__ import annotations

import pytest

from sql_lens.model import (
    BinOp,
    Delete,
    Func,
    Insert,
    Literal,
    RawStatement,
    Select,
    Subquery,
    SubqueryRef,
    Update,
)
from sql_lens.parser import ParseError, parse_all, parse_one, walk


def test_select_columns_and_alias():
    node = parse_one("SELECT id, name AS label FROM users")
    assert isinstance(node, Select)
    assert len(node.columns) == 2
    assert node.columns[1].alias == "label"


def test_from_and_join_shape():
    node = parse_one("SELECT a.id FROM a JOIN b ON b.a_id = a.id")
    assert len(node.from_items) == 1
    assert node.joins[0].kind == "INNER"
    assert isinstance(node.joins[0].on, BinOp)


def test_left_and_cross_joins_are_distinguished():
    left = parse_one("SELECT 1 FROM a LEFT JOIN b ON a.id = b.id")
    cross = parse_one("SELECT 1 FROM a CROSS JOIN b")
    assert left.joins[0].kind == "LEFT"
    assert cross.joins[0].kind == "CROSS"


def test_implicit_join_without_on_has_no_condition():
    node = parse_one("SELECT 1 FROM a JOIN b")
    assert node.joins[0].on is None


def test_select_star_and_qualified_star():
    node = parse_one("SELECT *, a.* FROM a")
    assert node.columns[0].is_star
    assert node.columns[1].is_star
    assert node.columns[0].expr.table is None
    assert node.columns[1].expr.table == "a"


def test_where_and_group_by_and_having():
    node = parse_one(
        "SELECT kind, count(*) FROM t WHERE live GROUP BY kind HAVING count(*) > 2"
    )
    assert isinstance(node.where, object)
    assert len(node.group_by) == 1
    assert node.having is not None


def test_limit_and_offset():
    node = parse_one("SELECT 1 FROM t LIMIT 10 OFFSET 20")
    assert isinstance(node.limit, Literal)
    assert isinstance(node.offset, Literal)


def test_mysql_limit_offset_comma_form():
    node = parse_one("SELECT 1 FROM t LIMIT 20, 10", "mysql")
    assert node.offset is not None and node.limit is not None


def test_ansi_fetch_first():
    node = parse_one("SELECT 1 FROM t OFFSET 5 ROWS FETCH FIRST 3 ROWS ONLY", "ansi")
    assert node.limit is not None


def test_cte_is_attached_to_select():
    node = parse_one("WITH recent AS (SELECT id FROM t) SELECT id FROM recent")
    assert len(node.ctes) == 1
    assert node.ctes[0].name == "recent"


def test_recursive_cte_flag():
    node = parse_one("WITH RECURSIVE nums AS (SELECT 1) SELECT * FROM nums")
    assert node.ctes[0].recursive is True


def test_cte_column_list():
    node = parse_one("WITH x(a, b) AS (SELECT 1, 2) SELECT a FROM x")
    assert node.ctes[0].columns == ["a", "b"]


def test_subquery_in_from():
    node = parse_one("SELECT s.id FROM (SELECT id FROM t) AS s")
    assert isinstance(node.from_items[0], SubqueryRef)
    assert node.from_items[0].alias == "s"


def test_subquery_in_where():
    node = parse_one("SELECT id FROM t WHERE id IN (SELECT id FROM other)")
    assert isinstance(node.where.right, Subquery)


def test_union_collects_set_operations():
    node = parse_one("SELECT 1 UNION ALL SELECT 2")
    assert node.set_ops[0][0] == "UNION"


def test_insert_values_and_columns():
    node = parse_one("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')")
    assert isinstance(node, Insert)
    assert node.columns == ["a", "b"]
    assert len(node.rows) == 2


def test_insert_select():
    node = parse_one("INSERT INTO t (a) SELECT id FROM s")
    assert node.select is not None


def test_update_assignments_and_where():
    node = parse_one("UPDATE t SET a = 1, b = a + 1 WHERE id = 5")
    assert isinstance(node, Update)
    assert [name for name, _ in node.assignments] == ["a", "b"]
    assert node.where is not None


def test_delete_without_where():
    node = parse_one("DELETE FROM t")
    assert isinstance(node, Delete)
    assert node.where is None


def test_ddl_becomes_raw_statement_with_tokens():
    node = parse_one("CREATE TABLE t (id int);")
    assert isinstance(node, RawStatement)
    assert node.upper_words[:2] == ["CREATE", "TABLE"]


def test_multiple_statements_and_semicolons():
    nodes = parse_all("SELECT 1; SELECT 2;;\nSELECT 3;")
    assert len(nodes) == 3


def test_postgres_cast():
    node = parse_one("SELECT id::bigint FROM t")
    assert node.columns[0].expr.__class__.__name__ == "Cast"


def test_dollar_quoted_string_is_a_literal():
    node = parse_one("SELECT $tag$hello$tag$ AS greeting")
    assert isinstance(node.columns[0].expr, Literal)


def test_function_with_distinct_and_star():
    distinct = parse_one("SELECT count(DISTINCT id) FROM t").columns[0].expr
    star = parse_one("SELECT count(*) FROM t").columns[0].expr
    assert isinstance(distinct, Func) and distinct.distinct is True
    assert isinstance(star, Func) and star.star is True


def test_case_and_between_and_like_parse():
    node = parse_one(
        "SELECT CASE WHEN a THEN 1 ELSE 0 END FROM t "
        "WHERE x BETWEEN 1 AND 2 AND name LIKE 'ab%'"
    )
    assert node.where is not None


def test_window_function_captures_over():
    fn = parse_one("SELECT row_number() OVER (PARTITION BY a) FROM t").columns[0].expr
    assert isinstance(fn, Func)
    assert "PARTITION BY" in fn.window


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "sqlite", "sqlserver", "ansi"])
def test_simple_query_parses_in_every_dialect(dialect):
    node = parse_one("SELECT a, b FROM t WHERE a = 1 ORDER BY b", dialect)
    assert isinstance(node, Select)


def test_parser_error_message_has_position_and_token():
    with pytest.raises(ParseError) as excinfo:
        parse_one("SELECT FROM t WHERE")
    message = str(excinfo.value)
    assert "line 1" in message
    assert "column" in message


def test_missing_paren_is_reported():
    with pytest.raises(ParseError):
        parse_one("SELECT count(id FROM t")


def test_parse_one_rejects_multiple_statements():
    with pytest.raises(ParseError) as excinfo:
        parse_one("SELECT 1; SELECT 2;")
    assert "exactly one" in str(excinfo.value)


def test_parse_one_rejects_empty_input():
    with pytest.raises(ParseError) as excinfo:
        parse_one("   -- just a comment")
    assert "no SQL statement" in str(excinfo.value)


def test_trailing_comma_in_select_list_is_an_error():
    with pytest.raises(ParseError) as excinfo:
        parse_one("SELECT id, FROM t")
    assert "trailing comma" in str(excinfo.value)


def test_walk_visits_nested_nodes():
    node = parse_one("SELECT a.id FROM a JOIN b ON b.id = a.id WHERE a.x IN (SELECT 1)")
    kinds = {type(n).__name__ for n in walk(node)}
    assert {"Select", "TableRef", "Join", "BinOp", "Subquery"} <= kinds


def test_alias_without_as_keyword():
    node = parse_one("SELECT id label FROM t")
    assert node.columns[0].alias == "label"


def test_quoted_identifier_keeps_case():
    node = parse_one('SELECT "UserId" FROM "Users"')
    assert node.columns[0].expr.name == "UserId"
    assert node.from_items[0].name == "Users"
