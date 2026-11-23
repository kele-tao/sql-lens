"""Generate examples/explain_orders.json from a REAL PostgreSQL run.

This script is committed so the bundled plan example is reproducible: it starts
a throwaway PostgreSQL (the pgserver wheel bundles the binaries), creates a demo
schema, runs ANALYZE, and captures EXPLAIN (ANALYZE, FORMAT JSON) for the query
in examples/slow_orders.sql.

pgserver is a development-only dependency; sql-lens itself is pure stdlib.

    pip install pgserver
    python benchmarks/make_plan_fixture.py
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pgserver

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "examples" / "explain_orders.json"

DDL = """
CREATE TABLE customers (
    id          bigserial PRIMARY KEY,
    email       text NOT NULL,
    country     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE orders (
    id           bigserial PRIMARY KEY,
    customer_id  bigint NOT NULL REFERENCES customers (id),
    status       text NOT NULL,
    total_cents  integer NOT NULL,
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
INSERT INTO customers (email, country, created_at)
SELECT 'user' || i || '@example.com',
       (ARRAY['DE','US','JP','BR','IN'])[1 + (i % 5)],
       now() - (i || ' days')::interval
FROM generate_series(1, 20000) AS i;
INSERT INTO orders (customer_id, status, total_cents, note, created_at)
SELECT 1 + (i % 20000),
       (ARRAY['new','paid','shipped','cancelled'])[1 + (i % 4)],
       500 + (i * 37) % 90000,
       CASE WHEN i % 97 = 0 THEN 'gift' ELSE NULL END,
       now() - ((i % 900) || ' days')::interval
FROM generate_series(1, 120000) AS i;
ANALYZE customers;
ANALYZE orders;
"""

UNALIGNED = "\\pset format unaligned\n\\pset tuples_only on\n"

QUERY = """
EXPLAIN (ANALYZE, FORMAT JSON)
SELECT c.email,
       c.country,
       count(o.id)          AS orders,
       sum(o.total_cents)   AS revenue
FROM customers AS c
JOIN orders AS o ON o.customer_id = c.id
WHERE lower(c.email) = 'user42@example.com'
  AND o.note LIKE '%gift%'
  AND o.status NOT IN (SELECT status FROM orders WHERE status = 'cancelled')
GROUP BY c.email, c.country
ORDER BY revenue DESC;
"""


def extract_json(text: str) -> str:
    """Pull the first complete JSON array/object out of psql output."""
    text = text.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    raise SystemExit("psql did not return a JSON plan:\n" + text[:1000])


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        server = pgserver.get_server(pathlib.Path(tmp) / "pgdata", cleanup_mode="delete")
        try:
            out = server.psql(DDL)
            if "ERROR" in out:
                raise SystemExit("postgres rejected the DDL:\n" + out[-2000:])
            plan_text = server.psql(UNALIGNED + QUERY)
        finally:
            server.cleanup()
    payload = json.loads(extract_json(plan_text))
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())