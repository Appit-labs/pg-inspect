#!/usr/bin/env python3
"""Read-only Postgres inspector. Shells out to psql; no Python dependencies.

    pg.py tables [--schema public]
    pg.py schema TABLE
    pg.py q "SELECT ..."            # any read-only query
    pg.py explain "SELECT ..."      # EXPLAIN (ANALYZE off) -- safe on prod
    pg.py search PATTERN            # find tables/columns by name
    pg.py migrations                # alembic/prisma/django migration state
    pg.py sizes [--limit 15]
    pg.py locks
    pg.py activity
    pg.py doctor                    # show what would connect, credentials masked
    pg.py self-test

Connection: --url postgres://... | $DATABASE_URL | libpq $PG* variables.
Every statement runs in a READ ONLY transaction with a 15s timeout; anything that
is not a single read statement is refused before connecting.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

READ_OK = ("select", "with", "table", "values", "explain", "show")
WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|copy|vacuum|"
    r"refresh|reindex|cluster|call|do|lock|commit|rollback|savepoint)\b", re.I)


class Refused(Exception):
    pass


def guard(sql: str) -> str:
    """Return the statement if it is a single read statement, else raise."""
    body = re.sub(r"^\s*(--[^\n]*\n\s*|/\*.*?\*/\s*)*", "", sql, flags=re.S).strip()
    if not body:
        raise Refused("empty statement")
    if body.rstrip(";").count(";"):
        raise Refused("only one statement per call")
    body = body.rstrip(";").strip()
    if not body.lower().startswith(READ_OK):
        raise Refused(f"only read statements ({', '.join(READ_OK)}) are allowed")
    if WRITE_WORDS.search(re.sub(r"'(?:[^']|'')*'", "''", body)):
        raise Refused("statement mentions a write keyword; refused")
    return body


def mask(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.password is None:
            return url
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return "<unparsable connection string>"


def connection() -> str | None:
    return os.environ.get("DATABASE_URL") or None


def which_psql() -> str:
    exe = shutil.which("psql")
    if not exe:
        raise SystemExit("psql not found on PATH: install the postgres client")
    return exe


def run(sql: str, tuples_only: bool = False) -> str:
    try:
        stmt = guard(sql)
    except Refused as exc:
        raise SystemExit(f"refused: {exc}")
    exe = which_psql()
    cmd = [exe, "-X", "-q", "--pset=pager=off", "-v", "ON_ERROR_STOP=1",
           "-P", "null=(null)"]
    if tuples_only:
        cmd += ["-tA"]
    else:
        cmd += ["-A", "-F", " | "]
    url = connection()
    if url:
        cmd += ["-d", url]
    cmd += ["-c", "SET default_transaction_read_only=on",
            "-c", "SET statement_timeout='15s'",
            "-c", stmt]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, "PGCONNECT_TIMEOUT": "10"})
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "psql failed")
    return proc.stdout.rstrip("\n")


def header(title: str) -> None:
    print(f"# {title}")


def cmd_tables(schema: str) -> int:
    header(f"tables in {schema}")
    print(run(f"""
        select c.relname as table,
               c.reltuples::bigint as approx_rows,
               pg_size_pretty(pg_total_relation_size(c.oid)) as size
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind in ('r','p') and n.nspname = {literal(schema)}
        order by pg_total_relation_size(c.oid) desc limit 200"""))
    return 0


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cmd_schema(table: str) -> int:
    schema, _, name = table.rpartition(".")
    schema = schema or "public"
    header(f"schema {schema}.{name}")
    print(run(f"""
        select column_name, data_type, is_nullable, coalesce(column_default,'') as default
        from information_schema.columns
        where table_schema = {literal(schema)} and table_name = {literal(name)}
        order by ordinal_position"""))
    print()
    header("indexes")
    print(run(f"""
        select indexname, indexdef from pg_indexes
        where schemaname = {literal(schema)} and tablename = {literal(name)}"""))
    return 0


def cmd_search(pattern: str) -> int:
    header(f"objects matching {pattern}")
    print(run(f"""
        select table_schema, table_name, column_name, data_type from information_schema.columns
        where column_name ilike {literal('%' + pattern + '%')}
           or table_name ilike {literal('%' + pattern + '%')}
        order by table_name, ordinal_position limit 100"""))
    return 0


def cmd_migrations() -> int:
    header("migration state")
    probes = [
        ("alembic_version", "select version_num from alembic_version"),
        ("django_migrations (last 5)", "select app, name, applied from django_migrations order by applied desc limit 5"),
        ("_prisma_migrations (last 5)", "select migration_name, finished_at from _prisma_migrations order by finished_at desc nulls last limit 5"),
    ]
    for label, sql in probes:
        try:
            out = run(sql)
        except SystemExit:
            continue
        print(f"- {label}:\n{out or '  (empty)'}")
    return 0


def cmd_sizes(limit: int) -> int:
    header("largest relations")
    print(run(f"""
        select relname as relation, pg_size_pretty(pg_total_relation_size(oid)) as total,
               pg_size_pretty(pg_relation_size(oid)) as heap
        from pg_class where relkind in ('r','p','m')
        order by pg_total_relation_size(oid) desc limit {int(limit)}"""))
    return 0


def cmd_locks() -> int:
    header("blocked / blocking")
    print(run("""
        select pid, wait_event_type, wait_event, state,
               now() - query_start as waited, left(query, 90) as query
        from pg_stat_activity
        where wait_event_type is not null and state <> 'idle'
        order by waited desc nulls last limit 20"""))
    return 0


def cmd_activity() -> int:
    header("activity")
    print(run("""
        select state, count(*) as n, max(now() - state_change) as longest
        from pg_stat_activity where backend_type = 'client backend'
        group by state order by n desc"""))
    return 0


def cmd_doctor() -> int:
    url = connection()
    print(f"psql: {shutil.which('psql') or 'MISSING'}")
    print(f"connection: {mask(url) if url else 'libpq $PG* env vars'}")
    if not url and not os.environ.get("PGHOST"):
        print("warning: no DATABASE_URL and no PGHOST; psql will try a local socket")
    print(f"db name: {os.environ.get('PGDATABASE') or (urlsplit(url).path.lstrip('/') if url else '<default>')}")
    return 0


def self_test() -> int:
    ok = ["SELECT 1", "select * from users", "  -- lead\n select 1",
          "with a as (select 1) select * from a", "EXPLAIN SELECT 1",
          "select * from t where name = 'update'", "TABLE users"]
    bad = ["delete from users", "drop table users", "select 1; drop table users",
           "update users set a=1", "insert into t values (1)", "vacuum", "",
           "select * from t for update", "create table x (a int)"]
    for sql in ok:
        guard(sql)
    for sql in bad:
        try:
            guard(sql)
        except Refused:
            continue
        raise AssertionError(f"guard allowed: {sql!r}")
    assert mask("postgres://u:p@h:5432/db") == "postgres://u:***@h:5432/db"
    assert mask("postgres://h/db") == "postgres://h/db"
    print("self-test ok")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = dict(a.lstrip("-").split("=", 1) for a in argv[1:] if a.startswith("--") and "=" in a)
    if "--url" in argv[1:]:
        i = argv.index("--url")
        if i + 1 < len(argv):
            os.environ["DATABASE_URL"] = argv[i + 1]
            args = [a for a in args if a != argv[i + 1]]
    if not args or args[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    cmd = args[0]
    if cmd == "self-test":
        return self_test()
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "tables":
        return cmd_tables(flags.get("schema", "public"))
    if cmd == "schema" and len(args) > 1:
        return cmd_schema(args[1])
    if cmd == "q" and len(args) > 1:
        print(run(" ".join(args[1:])))
        return 0
    if cmd == "explain" and len(args) > 1:
        print(run("EXPLAIN " + " ".join(args[1:])))
        return 0
    if cmd == "search" and len(args) > 1:
        return cmd_search(args[1])
    if cmd == "migrations":
        return cmd_migrations()
    if cmd == "sizes":
        return cmd_sizes(int(flags.get("limit", 15)))
    if cmd == "locks":
        return cmd_locks()
    if cmd == "activity":
        return cmd_activity()
    print(f"unknown or incomplete command: {' '.join(args)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
