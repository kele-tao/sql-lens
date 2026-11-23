"""Dialect-aware vocabulary: quoting, limits, and reserved words.

sql-lens parses one practical SQL grammar and switches behaviour per dialect.
The differences that actually change a query are captured here:

* how identifiers are quoted (double quotes vs backticks vs square brackets),
* how a row limit is written (LIMIT vs TOP vs FETCH FIRST),
* which words may not be used as bare identifiers (reserved words),
* which non-standard string syntax is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A practical subset of each engine's reserved words. These are the words that
# show up in real queries as column names (order, user, group, value, ...).
_BT = chr(96)

_COMMON_RESERVED = frozenset(
    """
    all and any as asc between by case cast check collate column constraint create
    cross current_date current_time current_timestamp default delete desc distinct
    drop else end except exists false fetch for foreign from full grant group having
    in inner insert intersect into is join left like limit natural not null offset on
    or order outer primary references right select set table then to true union unique
    update using values when where with
    """.split()
)

_POSTGRES_RESERVED = _COMMON_RESERVED | frozenset(
    """
    analyse analyze array asymmetric both current_user deferrable do freezone
    ilike initially lateral leading localtime localtimestamp only overlaps placing
    returning similar some symmetric variadic window
    """.split()
)

_MYSQL_RESERVED = _COMMON_RESERVED | frozenset(
    """
    accessible add auto_increment bigint blob change character convert database
    decimal describe distinctrow double escaped explain float force ignore index
    infile int integer interval keys kill lock long match mediumint modify no
    option optionally outfile purge read real regexp rename replace require rlike
    schema show smallint spatial specific sql_calc_found_rows sql_select
    sqlexception sqlstate sqlwarning starting straight_join terminate text tinyint
    trailing trigger undo unlock unsigned usage varbinary varchar write xor zerofill
    """.split()
)

_SQLITE_RESERVED = _COMMON_RESERVED | frozenset(
    """
    abort action add after alter always attach before begin cascade conflict database
    deferred detach each exclusive explain fail glob if ignore index indexed initially
    instead plan pragma query raise recursive reindex release rename replace restrict
    rollback row savepoint temp temporary trigger vacuum virtual without
    """.split()
)

_SQLSERVER_RESERVED = _COMMON_RESERVED | frozenset(
    """
    add bigint bit char cursor decimal exec execute identity int nchar ntext numeric
    nvarchar real rowcount rowguidcol smallint text top tran try_convert uniqueidentifier
    varbinary varchar while
    """.split()
)


@dataclass(frozen=True)
class Dialect:
    """Static description of one SQL dialect."""

    name: str
    label: str
    quote_pairs: tuple[tuple[str, str], ...]
    reserved: frozenset[str]
    limit_style: str  # "limit_offset", "top", "fetch"
    supports_ilike: bool = False
    supports_returning: bool = False
    backslash_escapes: bool = False
    supports_dollar_quote: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def quote_for(self, word: str) -> str:
        """Quote *word* using the dialect's preferred quoting character."""
        opener, closer = self.quote_pairs[0]
        escaped = word.replace(closer, closer * 2)
        return opener + escaped + closer


DIALECTS: dict[str, Dialect] = {
    "postgres": Dialect(
        name="postgres",
        label="PostgreSQL",
        quote_pairs=(('"', '"'),),
        reserved=_POSTGRES_RESERVED,
        limit_style="limit_offset",
        supports_ilike=True,
        supports_returning=True,
        supports_dollar_quote=True,
        notes=("LIMIT ... OFFSET", "::type casts", "ILIKE", "RETURNING"),
    ),
    "mysql": Dialect(
        name="mysql",
        label="MySQL",
        quote_pairs=((_BT, _BT),),
        reserved=_MYSQL_RESERVED,
        limit_style="limit_offset",
        backslash_escapes=True,
        notes=("LIMIT offset, count", "backtick identifiers", "backslash escapes"),
    ),
    "sqlite": Dialect(
        name="sqlite",
        label="SQLite",
        quote_pairs=(('"', '"'), (_BT, _BT), ("[", "]")),
        reserved=_SQLITE_RESERVED,
        limit_style="limit_offset",
        notes=("flexible quoting", "LIMIT ... OFFSET"),
    ),
    "sqlserver": Dialect(
        name="sqlserver",
        label="SQL Server (T-SQL)",
        quote_pairs=(("[", "]"), ('"', '"')),
        reserved=_SQLSERVER_RESERVED,
        limit_style="top",
        notes=("SELECT TOP n", "bracket identifiers"),
    ),
    "ansi": Dialect(
        name="ansi",
        label="ANSI SQL",
        quote_pairs=(('"', '"'),),
        reserved=_COMMON_RESERVED,
        limit_style="fetch",
        notes=("OFFSET ... FETCH FIRST n ROWS ONLY",),
    ),
}

ALIASES = {
    "postgresql": "postgres",
    "pg": "postgres",
    "psql": "postgres",
    "mariadb": "mysql",
    "sqlite3": "sqlite",
    "mssql": "sqlserver",
    "tsql": "sqlserver",
    "standard": "ansi",
    "sql": "ansi",
}


def get_dialect(name: str | None) -> Dialect:
    """Resolve a dialect name (and common aliases) or raise a helpful error."""
    if not name:
        return DIALECTS["postgres"]
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key not in DIALECTS:
        choices = ", ".join(sorted(DIALECTS))
        raise ValueError(f"unknown dialect {name!r}; choose one of: {choices}")
    return DIALECTS[key]