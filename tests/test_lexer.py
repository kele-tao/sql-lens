"""Tokenizer tests: positions, quoting, comments and error paths."""

from __future__ import annotations

import pytest

from sql_lens.lexer import (
    COMMENT,
    EOF,
    NUMBER,
    OP,
    PLACEHOLDER,
    PUNCT,
    QUOTED,
    STRING,
    WORD,
    LexError,
    strip_comments,
    tokenize,
)


def kinds(sql, dialect="postgres"):
    return [(t.kind, t.value) for t in tokenize(sql, dialect) if t.kind != EOF]


def test_simple_statement_tokens():
    assert kinds("SELECT 1") == [(WORD, "SELECT"), (NUMBER, "1")]


def test_positions_are_one_based_and_track_lines():
    tokens = tokenize("SELECT id\nFROM t")
    assert [(t.line, t.col) for t in tokens[:-1]] == [(1, 1), (1, 8), (2, 1), (2, 6)]


def test_newline_before_flag_is_preserved():
    tokens = tokenize("SELECT\n  id")
    assert tokens[0].newline_before is False
    assert tokens[1].newline_before is True
    assert tokens[1].space_before is True


def test_string_literal_with_doubled_quote():
    assert kinds("SELECT 'it''s'") == [(WORD, "SELECT"), (STRING, "it''s")]


def test_postgres_dollar_quoted_string():
    assert kinds("SELECT $tag$ a 'b' $tag$") == [(WORD, "SELECT"), (STRING, " a 'b' ")]


def test_dollar_quote_requires_postgres():
    with pytest.raises(LexError):
        tokenize("SELECT $body$ x $body$", "mysql")


def test_mysql_backtick_identifier_is_quoted():
    tokens = tokenize("SELECT " + chr(96) + "order" + chr(96) + " FROM t", "mysql")
    assert tokens[1].kind == QUOTED
    assert tokens[1].value == "order"
    assert tokens[1].quoted is True


def test_mysql_backslash_escape_inside_string():
    assert kinds("SELECT 'a" + chr(92) + "'b'", "mysql") == [
        (WORD, "SELECT"),
        (STRING, "a" + chr(92) + "'b"),
    ]


def test_sqlserver_bracket_identifier():
    tokens = tokenize("SELECT [user name] FROM t", "sqlserver")
    assert tokens[1].kind == QUOTED
    assert tokens[1].value == "user name"


def test_operators_longest_match_first():
    values = [v for k, v in kinds("a ->> b :: int") if k == OP]
    assert "->>" in values
    assert "::" in values


def test_placeholder_forms_are_one_kind():
    assert [v for k, v in kinds("WHERE a = $1 AND b = ? AND c = :name AND d = %s") if k == PLACEHOLDER] == [
        "$1",
        "?",
        ":name",
        "%s",
    ]


def test_comments_are_tokens_and_can_be_stripped():
    tokens = tokenize("SELECT 1 -- why\n /* block */ , 2")
    assert any(t.kind == COMMENT for t in tokens)
    stripped = strip_comments(tokens)
    assert all(t.kind != COMMENT for t in stripped)
    assert [t.value for t in stripped if t.kind != EOF] == ["SELECT", "1", ",", "2"]


def test_nested_block_comment_in_postgres():
    tokens = tokenize("SELECT /* a /* b */ c */ 1")
    comments = [t for t in tokens if t.kind == COMMENT]
    assert len(comments) == 1
    assert comments[0].value == "/* a /* b */ c */"


def test_flat_block_comment_in_mysql():
    tokens = tokenize("SELECT /* a /* b */ 1", "mysql")
    comments = [t for t in tokens if t.kind == COMMENT]
    assert comments[0].value == "/* a /* b */"


def test_unterminated_string_is_a_lex_error_with_position():
    with pytest.raises(LexError) as excinfo:
        tokenize("SELECT 'oops")
    assert "unterminated string" in str(excinfo.value)
    assert excinfo.value.line == 1


def test_unterminated_block_comment_is_a_lex_error():
    with pytest.raises(LexError) as excinfo:
        tokenize("SELECT /* nope")
    assert "unterminated block comment" in str(excinfo.value)


def test_unexpected_character_is_reported_with_position():
    with pytest.raises(LexError) as excinfo:
        tokenize("SELECT \u00a7")
    assert "\u00a7" in str(excinfo.value)
    assert excinfo.value.col == 8


def test_smart_quote_gets_a_helpful_message():
    with pytest.raises(LexError) as excinfo:
        tokenize("SELECT \u201cname\u201d FROM t")
    assert "smart quote" in str(excinfo.value)


def test_unicode_identifiers_are_words():
    assert kinds("SELECT \u7528\u6237\u540d FROM t") == [
        (WORD, "SELECT"),
        (WORD, "\u7528\u6237\u540d"),
        (WORD, "FROM"),
        (WORD, "t"),
    ]


def test_empty_input_yields_only_eof():
    assert [t.kind for t in tokenize("   \n\t ")] == [EOF]


def test_scientific_and_hex_numbers():
    assert [v for k, v in kinds("SELECT 1.5e3, 0x1F") if k == NUMBER] == ["1.5e3", "0x1F"]


def test_star_is_punctuation():
    # '*' is punctuation so that SELECT * and count(*) both parse as a star,
    # and the parser decides whether it is a wildcard or multiplication.
    assert kinds("SELECT count(*) FROM t")[3] == (PUNCT, "*")
    assert (PUNCT, "*") in kinds("SELECT a * b FROM t")


def test_repeated_tokenize_is_deterministic():
    sql = "SELECT a.b, c FROM t WHERE x = $1 -- c\nORDER BY 1"
    assert kinds(sql) == kinds(sql)