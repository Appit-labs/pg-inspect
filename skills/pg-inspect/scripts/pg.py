#!/usr/bin/env python3
"""pg-inspect — a read-only Postgres inspector for agents and humans. Shells out to psql.

    SCHEMA & SHAPE
      overview                    the ten-second picture: size, tables, busiest, waits
      tables [--schema s] [--like p]   rows, sizes, bars, dead tuples
      schema TABLE                columns, defaults, indexes, constraints, foreign keys
      ddl TABLE                   copy-pasteable CREATE TABLE + indexes
      erd [--schema s]            entity diagram (mermaid by default, --ascii for terminal)
      fingerprint [--against URL] schema hash; --against prints the drift
      fk [--schema s]             the join graph, edge by edge
      sample TABLE [--limit N]    a few real rows

    QUERIES
      q "SELECT ..."              one read statement, box table, timing
      explain "SELECT ..."        plan tree, cost bars, warnings (never executes)
      slow [--by total|mean]      pg_stat_statements, worst first
      long-running [--minutes 5]  sessions stuck in a query

    HEALTH
      audit                       every check below, prioritised, one report
      indexes [--schema s]        size, scans, unique; flags unused and duplicates
      missing-fk-indexes          foreign keys with no supporting index
      bloat                       dead tuples and wasted bytes per table
      vacuum                      autovacuum lag and dead-tuple backlog
      seq-scans                   tables scanned sequentially more than by index
      cache                       buffer and index hit ratios, temp spill, deadlocks
      xid                         transaction-ID age (wraparound) and oldest xmin
      sequences                   sequences close to exhaustion
      replication                 replicas and lag
      connections                 who is connected, against max_connections

    SAFETY & OPS
      grants TABLE | rls          privileges per table; row-level security coverage
      locks                       blocking tree: who waits on whom
      activity                    connection states
      sizes [--limit N]           biggest relations, table vs index bytes
      migrations                  alembic / django / prisma state
      search PATTERN              find a table or column by name
      doctor                      what would connect, credentials masked
      self-test

Connection: --url postgres://… | $DATABASE_URL | libpq $PG* variables.
Formats: --json | --csv | --md (markdown table) on any command; colour only on a TTY
(respect NO_COLOR). Every statement runs READ ONLY with a 15s timeout; anything that is
not a single read statement is refused before connecting.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

READ_OK = ("select", "with", "table", "values", "explain", "show")
WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|copy|vacuum|"
    r"refresh|reindex|cluster|call|do|lock|commit|rollback|savepoint)\b", re.I)
FMT = "table"


class Refused(Exception):
    """A statement this tool will not run."""


# ------------------------------------------------------------------ rendering

class Style:
    def __init__(self, on: bool):
        self.on = on

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, t): return self(t, "1")
    def dim(self, t): return self(t, "2")
    def cyan(self, t): return self(t, "36")
    def green(self, t): return self(t, "32")
    def yellow(self, t): return self(t, "33")
    def red(self, t): return self(t, "31")
    def magenta(self, t): return self(t, "35")


S = Style(False) if os.environ.get("NO_COLOR") else Style(sys.stdout.isatty())
SEEN: dict[str, float] = {}


def human_bytes(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def bar(value: float, biggest: float, width: int = 16) -> str:
    if biggest <= 0 or value <= 0:
        return "·" * 0
    return "█" * max(1, round(width * value / biggest))


def esc(text: str) -> str:
    return re.sub(r"\033\[\d+m", "", str(text))


def emit(headers: list[str], rows: list[list[str]], align: dict[int, str] | None = None,
         bars: list[float] | None = None, bar_col: int | None = None,
         footer_note: str = "") -> None:
    """Render one result set in the selected format (table | json | csv | md)."""
    align = align or {}
    if FMT == "json":
        print(json.dumps([dict(zip(headers, r)) for r in rows], indent=2))
        return
    if FMT == "csv":
        import csv
        w = csv.writer(sys.stdout)
        w.writerow(headers)
        w.writerows(rows)
        return
    if FMT == "md":
        print("| " + " | ".join(headers) + " |")
        print("| " + " | ".join("---" for _ in headers) + " |")
        for r in rows:
            print("| " + " | ".join(str(c) for c in r) + " |")
        return
    if not rows:
        print(S.dim("(no rows)"))
        return
    if bar_col is not None and bars:
        headers = headers[:bar_col + 1] + [""] + headers[bar_col + 1:]
        biggest = max(bars) if bars else 0
        rows = [[*r[:bar_col + 1], S.green(bar(bars[i], biggest)) or S.dim("·"), *r[bar_col + 1:]]
                for i, r in enumerate(rows)]
    widths = [len(esc(h)) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(esc(c)))
    if bar_col is not None and bars:
        widths[bar_col + 1] = 16

    def rule(l, m, r):
        return S.dim(l + m.join("─" * (w + 2) for w in widths) + r)

    print(rule("┌", "┬", "┐"))
    print(S.dim("│") + "".join(" " + S.bold(h.ljust(widths[i])) + " " + S.dim("│")
                               for i, h in enumerate(headers)))
    print(rule("├", "┼", "┤"))
    for idx, r in enumerate(rows):
        cells = list(r)
        print(S.dim("│") + "".join(
            " " + (str(c).rjust(widths[i]) if align.get(i) == "r" else str(c).ljust(widths[i]))
            + " " + S.dim("│") for i, c in enumerate(cells)))
    print(rule("└", "┴", "┘"))
    if footer_note:
        print(S.dim(footer_note))


def timed(key: str, n: int, started: float, extra: str = "") -> None:
    ms = (time.time() - started) * 1000
    bits = [f"{n} row{'s' if n != 1 else ''}", f"{ms:.0f} ms"]
    if extra:
        bits.insert(0, extra)
    if FMT == "table":
        print(S.dim("— " + " · ".join(bits)))


def note(msg: str) -> None:
    if FMT == "table":
        print(S.dim(msg))


def warn_line(msg: str) -> None:
    if FMT == "table":
        print(S.yellow("! ") + msg)


# -------------------------------------------------------------------- plumbing

def guard(sql: str) -> str:
    body = re.sub(r"^\s*(--[^\n]*\n\s*|/\*.*?\*/\s*)*", "", sql, flags=re.S).strip()
    if not body:
        raise Refused("empty statement")
    if body.rstrip(";").count(";"):
        raise Refused("only one statement per call")
    body = body.rstrip(";").strip()
    if not body.lower().startswith(READ_OK):
        raise Refused("only read statements (select, with, table, values, explain, show) are allowed")
    if WRITE_WORDS.search(re.sub(r"'(?:[^']|'')*'", "''", body)):
        raise Refused("statement mentions a write keyword; refused")
    return body


def mask(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.password is None:
            return url
        return urlunsplit((parts.scheme, parts.netloc.replace(f":{parts.password}@", ":***@"),
                           parts.path, parts.query, parts.fragment))
    except ValueError:
        return "<unparsable connection string>"


def psql(argv_extra: list[str], sql: str, url: str | None = None) -> subprocess.CompletedProcess:
    exe = shutil.which("psql")
    if not exe:
        raise SystemExit("psql not found on PATH: install the postgres client")
    cmd = [exe, "-X", "-q", "--pset=pager=off", "-v", "ON_ERROR_STOP=1", *argv_extra]
    env = {**os.environ, "PGCONNECT_TIMEOUT": "10"}
    target = url or os.environ.get("DATABASE_URL")
    if target:
        cmd += ["-d", target]
        if url:
            env.pop("PGHOST", None)
    cmd += ["-c", "SET default_transaction_read_only=on",
            "-c", "SET statement_timeout='15s'",
            "-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "psql failed")
    return proc


def rows_of(sql: str) -> list[list[str]]:
    proc = psql(["-tA", "-F", "\t", "-P", "null=(null)"], guard(sql))
    return [ln.split("\t") for ln in proc.stdout.splitlines() if ln != ""]


def rows_with_header(sql: str) -> tuple[list[str], list[list[str]]]:
    proc = psql(["-A", "-F", "\t", "-P", "null=(null)"], guard(sql))
    lines = [ln for ln in proc.stdout.splitlines() if ln and not re.fullmatch(r"\(\d+ rows?\)", ln)]
    return (lines[0].split("\t"), [l.split("\t") for l in lines[1:]]) if lines else ([], [])


def truthy(v: str) -> bool:
    return str(v).strip().lower() in {"t", "true", "1", "yes", "on"}


def one(sql: str, default: str = "") -> str:
    rows = rows_of(sql)
    return rows[0][0] if rows and rows[0] else default


def lit(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def split_table(t: str) -> tuple[str, str]:
    if "." in t:
        s, _, n = t.partition(".")
        return s or "public", n
    return "public", t


def exists_query(schema: str, table: str) -> bool:
    return truthy(one(f"select to_regclass({lit(schema + '.' + table)}) is not null"))


# --------------------------------------------------------------------- schema

def cmd_overview() -> int:
    started = time.time()
    db = one("select current_database()")
    version = one("select version()").split(" on ")[0].replace("PostgreSQL ", "PG ")
    size = one("select pg_size_pretty(pg_database_size(current_database()))")
    tables, live = rows_of("""
        select count(*), coalesce(sum(n_live_tup),0)::text from pg_stat_user_tables""")[0]
    dead = one("select coalesce(sum(n_dead_tup),0)::text from pg_stat_user_tables")
    biggest = rows_of("""
        select c.relname, pg_size_pretty(pg_total_relation_size(c.oid))
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind in ('r','p','m') and n.nspname not in ('pg_catalog','information_schema')
        order by pg_total_relation_size(c.oid) desc limit 4""")
    wait, conns, maxconn = rows_of("""
        select (select count(*) from pg_stat_activity where wait_event_type='Lock')::text,
               (select count(*) from pg_stat_activity where backend_type='client backend')::text,
               current_setting('max_connections')""")[0]
    top = rows_of("""
        select left(regexp_replace(query,'\\s+',' ','g'),58), round(mean_exec_time::numeric,1)::text
        from pg_stat_statements order by total_exec_time desc limit 3""") if truthy(one("select count(*) from pg_extension where extname='pg_stat_statements'")) else []
    if FMT != "table":
        emit(["key", "value"], [["database", db], ["version", version], ["size", size], ["tables", tables],
                                ["live_rows", live], ["dead_rows", dead], ["connections", conns],
                                ["max_connections", maxconn], ["lock_waits", wait],
                                ["busiest", ", ".join(f"{n} {s}" for n, s in biggest)]])
        return 0
    print(S.bold("database   ") + S.cyan(db) + S.dim("  · " + version))
    print(S.bold("size       ") + f"{size}  {S.dim(f'· {tables} tables · {int(live):,} live rows · {int(dead):,} dead')}")
    print(S.bold("busiest    ") + ", ".join(f"{n} {s}" for n, s in biggest))
    locktxt = S.red(f"{wait} waiting on locks") if int(wait) else S.dim("no lock waits")
    print(S.bold("now        ") + f"{conns}/{maxconn} connections · " + locktxt)
    if top:
        print(S.bold("slowest    ") + f"{float(top[0][1]):.0f} ms mean  " + S.dim(top[0][0][:58]))
    timed("overview", 1, started)
    return 0


def cmd_tables(schema: str, like: str | None) -> int:
    started = time.time()
    where = f"n.nspname = {lit(schema)}"
    if like:
        where += f" and c.relname ilike {lit('%' + like + '%')}"
    rows = rows_of(f"""
        select c.relname, coalesce(s.n_live_tup,0)::text, pg_total_relation_size(c.oid)::text,
               coalesce(s.n_dead_tup,0)::text, coalesce(s.seq_scan,0)::text, coalesce(s.idx_scan,0)::text
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        left join pg_stat_user_tables s on s.relid=c.oid
        where c.relkind in ('r','p') and {where}
        order by pg_total_relation_size(c.oid) desc""")
    out = [[r[0], f"{int(r[1]):,}", human_bytes(int(r[2])), f"{int(r[3]):,}", f"{int(r[5]) - int(r[4]):,}"]
           for r in rows]
    emit(["table", "rows", "size", "dead", "idx−seq"], out, align={1: "r", 2: "r", 3: "r", 4: "r"},
         bars=[float(r[2]) for r in rows], bar_col=2,
         footer_note=f"— {schema} · {human_bytes(sum(int(r[2]) for r in rows))} · {len(rows)} tables")
    timed("tables", len(rows), started)
    return 0


def cmd_schema(t: str) -> int:
    schema, name = split_table(t)
    if not exists_query(schema, name):
        print(f"no such table: {schema}.{name}", file=sys.stderr)
        return 2
    started = time.time()
    cols = rows_of(f"""
        select column_name, data_type, is_nullable, coalesce(column_default,''),
               coalesce(character_maximum_length::text,'')
        from information_schema.columns where table_schema={lit(schema)} and table_name={lit(name)}
        order by ordinal_position""")
    idx = rows_of(f"select indexname, indexdef from pg_indexes where schemaname={lit(schema)} and tablename={lit(name)}")
    cons = rows_of(f"""
        select con.conname, con.contype, pg_get_constraintdef(con.oid)
        from pg_constraint con join pg_class c on c.oid=con.conrelid
        join pg_namespace n on n.oid=c.relnamespace
        where n.nspname={lit(schema)} and c.relname={lit(name)}""")
    rls = one(f"select relrowsecurity::text from pg_class c join pg_namespace n on n.oid=c.relnamespace "
              f"where n.nspname={lit(schema)} and c.relname={lit(name)}")
    emit(["column", "type", "null", "default"],
         [[c[0], c[1] + (f"({c[4]})" if c[4] else ""), "not null" if c[2] == "NO" else "null", c[3]] for c in cols])
    if FMT != "table":
        return 0
    if truthy(rls):
        print(S.magenta("\nrow-level security: enabled"))
    if cons:
        print("\n" + S.bold("constraints"))
        for n_, t_, d in cons:
            print(f"  {S.cyan(n_)}  {S.dim(d[:96])}")
    if idx:
        print("\n" + S.bold("indexes"))
        for n_, d in idx:
            print(f"  {S.cyan(n_)}  {S.dim(d[:96])}")
    timed("schema", len(cols), started)
    return 0


def cmd_ddl(t: str) -> int:
    schema, name = split_table(t)
    if not exists_query(schema, name):
        print(f"no such table: {schema}.{name}", file=sys.stderr)
        return 2
    cols = rows_of(f"""
        select column_name, data_type, is_nullable, coalesce(column_default,''),
               coalesce(character_maximum_length::text,''), coalesce(numeric_precision::text,''),
               coalesce(numeric_scale::text,'')
        from information_schema.columns where table_schema={lit(schema)} and table_name={lit(name)}
        order by ordinal_position""")
    defs = []
    for c, dt, nullable, default, charlen, prec, scale in cols:
        typ = f"{dt}({charlen})" if charlen else (f"{dt}({prec}{',' + scale if scale else ''})" if prec else dt)
        piece = f"    {c} {typ}"
        if default:
            piece += f" DEFAULT {default}"
        if nullable == "NO":
            piece += " NOT NULL"
        defs.append(piece)
    for (d,) in rows_of(f"""
            select pg_get_constraintdef(con.oid) from pg_constraint con
            join pg_class c on c.oid=con.conrelid join pg_namespace n on n.oid=c.relnamespace
            where n.nspname={lit(schema)} and c.relname={lit(name)} and con.contype in ('p','u','c','f')"""):
        defs.append(f"    {d}")
    print(f"CREATE TABLE {schema}.{name} (")
    print(",\n".join(defs))
    print(");")
    for _, d in rows_of(f"select indexname, indexdef from pg_indexes where schemaname={lit(schema)} and tablename={lit(name)}"):
        if "UNIQUE INDEX" in d and " PRIMARY KEY " in d:
            continue
        print(d.rstrip(";") + ";")
    return 0


def cmd_erd(schema: str) -> int:
    tables = rows_of(f"""
        select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind in ('r','p') and n.nspname={lit(schema)} order by 1""")
    cols = rows_of(f"""
        select c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
               case when pk.attnum is not null then 'PK' when fk.attnum is not null then 'FK' else '' end
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        join pg_attribute a on a.attrelid=c.oid and a.attnum>0 and not a.attisdropped
        left join (select conrelid, unnest(conkey) as attnum from pg_constraint where contype='p') pk
               on pk.conrelid=c.oid and pk.attnum=a.attnum
        left join (select conrelid, unnest(conkey) as attnum from pg_constraint where contype='f') fk
               on fk.conrelid=c.oid and fk.attnum=a.attnum
        where c.relkind in ('r','p') and n.nspname={lit(schema)} order by 1, a.attnum""")
    fks = rows_of(f"""
        select c.relname, a.attname, t.relname, ta.attname
        from pg_constraint con join pg_class c on c.oid=con.conrelid
        join pg_namespace n on n.oid=c.relnamespace join pg_class t on t.oid=con.confrelid
        join unnest(con.conkey, con.confkey) as k(ca, pa) on true
        join pg_attribute a on a.attrelid=c.oid and a.attnum=k.ca
        join pg_attribute ta on ta.attrelid=t.oid and ta.attnum=k.pa
        where con.contype='f' and n.nspname={lit(schema)} order by 1""")
    if FMT == "json":
        print(json.dumps({"tables": tables, "columns": cols, "foreign_keys": fks}, indent=2))
        return 0
    if FMT == "md" or os.environ.get("PG_ERD") == "mermaid":
        print("```mermaid")
        print("erDiagram")
        for child, col, parent, pcol in fks:
            print(f"  {child} }}o--|| {parent} : \"{col} → {pcol}\"")
        for tbl in [t[0] for t in tables]:
            print(f"  {tbl} {{")
            for t_, c, typ, kind in cols:
                if t_ == tbl:
                    print(f"    {typ.replace(' ', '_')} {c}" + (" PK" if kind == "PK" else ""))
            print("  }")
        print("```")
        return 0
    by_tbl: dict[str, list[tuple[str, str, str]]] = {}
    for t_, c, typ, kind in cols:
        by_tbl.setdefault(t_, []).append((c, typ, kind))
    print(S.bold(f"schema {schema}") + S.dim(f"  {len(tables)} tables · {len(fks)} foreign keys"))
    for tbl, items in by_tbl.items():
        print()
        print(S.cyan(tbl))
        for c, typ, kind in items:
            tag = S.magenta(" PK") if kind == "PK" else (S.yellow(" FK") if kind == "FK" else "")
            print(f"  {c:<26} {S.dim(typ)}{tag}")
    if fks:
        print()
        print(S.bold("edges"))
        for child, col, parent, pcol in fks:
            print(f"  {S.cyan(child)}.{col} {S.dim('→')} {S.cyan(parent)}.{pcol}")
    return 0


def schema_digest(url: str | None = None) -> tuple[str, set[str]]:
    columns = """select table_schema || '.' || table_name || '.' || column_name || ' ' || data_type
                 || ' null=' || is_nullable || ' default=' || coalesce(column_default,'-')
                 from information_schema.columns
                 where table_schema not in ('pg_catalog','information_schema') order by 1"""
    indexes = """select schemaname || '.' || tablename || ' ' || indexdef from pg_indexes
                 where schemaname not in ('pg_catalog','information_schema') order by 1"""
    lines: set[str] = set()
    for q in (columns, indexes):
        proc = psql(["-tA", "-F", "\t"], guard(q), url=url)
        for ln in proc.stdout.splitlines():
            if ln.strip():
                lines.add(ln.strip())
    digest = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:16]
    return digest, lines


def cmd_fingerprint(against: str | None) -> int:
    started = time.time()
    digest, lines = schema_digest()
    if not against:
        emit(["fingerprint", "objects"], [[digest, str(len(lines))]],
             footer_note=f"— {mask(os.environ.get('DATABASE_URL', 'libpq env'))}")
        if FMT == "table":
            note(f"compare two environments: pg.py fingerprint --against postgres://staging/…")
        timed("fingerprint", len(lines), started)
        return 0
    other, other_lines = schema_digest(against)
    added = sorted(other_lines - lines)
    removed = sorted(lines - other_lines)
    print(S.bold("here   ") + digest + S.dim(f"  {len(lines)} objects"))
    print(S.bold("against") + other + S.dim(f"  {mask(against)}"))
    if not added and not removed:
        print(S.green("\nno drift — the two schemas match"))
        return 0
    for line in removed:
        print(S.red("- ") + line[:120])
    for line in added:
        print(S.green("+ ") + line[:120])
    print(S.dim(f"\n{len(removed)} only here · {len(added)} only there"))
    return 1


# --------------------------------------------------------------------- indexes

def cmd_indexes(schema: str) -> int:
    started = time.time()
    rows = rows_of(f"""
        select s.relname, s.indexrelname, pg_relation_size(s.indexrelid)::text,
               coalesce(s.idx_scan,0)::text, i.indisunique::text, i.indisvalid::text,
               pg_get_indexdef(s.indexrelid)
        from pg_stat_user_indexes s join pg_index i on i.indexrelid=s.indexrelid
        join pg_class c on c.oid=s.relid join pg_namespace n on n.oid=c.relnamespace
        where n.nspname={lit(schema)} order by pg_relation_size(s.indexrelid) desc""")
    out = []
    for table, idx, size, scans, uniq, valid, definition in rows:
        flag = ""
        if not truthy(valid):
            flag = "INVALID"
        elif int(scans) == 0 and not truthy(uniq):
            flag = "never scanned"
        out.append([table, idx, human_bytes(int(size)), f"{int(scans):,}",
                    "yes" if truthy(uniq) else "", (S.red(flag) if FMT == "table" else flag) if flag else ""])
    emit(["table", "index", "size", "scans", "unique", "flag"], out, align={2: "r", 3: "r"},
         bars=[float(r[2]) for r in rows], bar_col=2)
    unused = [r for r in rows if int(r[3]) == 0 and not truthy(r[4])]
    timed("indexes", len(rows), started, f"{len(unused)} never scanned, non-unique")
    return 0


def cmd_unused_indexes() -> int:
    started = time.time()
    rows = rows_of("""
        select s.relname, s.indexrelname, pg_size_pretty(pg_relation_size(s.indexrelid)),
               coalesce(s.idx_scan,0)::text, pg_get_indexdef(s.indexrelid)
        from pg_stat_user_indexes s join pg_index i on i.indexrelid=s.indexrelid
        where s.idx_scan = 0 and not i.indisunique and i.indisvalid
        order by pg_relation_size(s.indexrelid) desc""")
    emit(["table", "index", "size", "scans", "definition"],
         [[r[0], r[1], r[2], r[3], r[4][:70]] for r in rows], align={2: "r", 3: "r"})
    if FMT == "table" and rows:
        note("each of these costs write throughput and disk, and has served no read since stats were reset")
    timed("unused-indexes", len(rows), started)
    return 0


def cmd_fk(schema: str) -> int:
    started = time.time()
    rows = rows_of(f"""
        select c.relname, a.attname, t.relname, ta.attname
        from pg_constraint con join pg_class c on c.oid=con.conrelid
        join pg_namespace n on n.oid=c.relnamespace join pg_class t on t.oid=con.confrelid
        join unnest(con.conkey, con.confkey) as k(ca, pa) on true
        join pg_attribute a on a.attrelid=c.oid and a.attnum=k.ca
        join pg_attribute ta on ta.attrelid=t.oid and ta.attnum=k.pa
        where con.contype='f' and n.nspname={lit(schema)} order by 1, 2""")
    if FMT == "table":
        for child, col, parent, pcol in rows:
            print(f"{S.cyan(child)}.{col} {S.dim('→')} {S.cyan(parent)}.{pcol}")
    else:
        emit(["child", "column", "parent", "parent_column"], [list(r) for r in rows])
    timed("fk", len(rows), started, schema)
    return 0


def cmd_duplicate_indexes() -> int:
    started = time.time()
    rows = rows_of("""
        select indrelid::regclass::text, array_to_string(array_agg(indexrelid::regclass::text), ', '),
               (regexp_replace(pg_get_indexdef(indexrelid), '^.*? USING ', '')) as cols, count(*)
        from pg_index group by indrelid, cols having count(*) > 1
        order by count(*) desc""")
    emit(["table", "indexes", "definition", "count"],
         [[r[0], r[1], r[2][:60], r[3]] for r in rows], align={3: "r"})
    timed("duplicate-indexes", len(rows), started)
    return 0


def cmd_missing_fk_indexes() -> int:
    started = time.time()
    rows = rows_of("""
        select c.relname, con.conname,
               (select string_agg(a.attname, ',') from unnest(con.conkey) k
                join pg_attribute a on a.attrelid=c.oid and a.attnum=k) as cols,
               t.relname
        from pg_constraint con join pg_class c on c.oid=con.conrelid
        join pg_namespace n on n.oid=c.relnamespace join pg_class t on t.oid=con.confrelid
        where con.contype='f' and n.nspname not in ('pg_catalog','information_schema')
          and not exists (
            select 1 from pg_index i
            where i.indrelid = con.conrelid
              and (i.indkey::int2[])[0:array_length(con.conkey,1)-1] = con.conkey)
        order by c.relname""")
    emit(["table", "constraint", "columns", "references"],
         [[r[0], r[1], r[2], r[3]] for r in rows])
    if FMT == "table" and rows:
        note("deleting a parent row scans the child table without these indexes")
    timed("missing-fk-indexes", len(rows), started)
    return 0


# ---------------------------------------------------------------------- health

def cmd_bloat() -> int:
    started = time.time()
    rows = rows_of("""
        select relname, n_live_tup::text, n_dead_tup::text,
               pg_total_relation_size(relid)::text,
               round(100.0 * n_dead_tup / greatest(n_live_tup + n_dead_tup, 1), 1)::text
        from pg_stat_user_tables where n_dead_tup > 0 order by n_dead_tup desc limit 20""")
    out = []
    for name, live, dead, size, pct in rows:
        waste = int(size) * int(dead) / max(int(live) + int(dead), 1)
        out.append([name, live, dead, human_bytes(waste), pct + "%"])
    emit(["table", "live", "dead", "≈wasted", "dead%"], out, align={1: "r", 2: "r", 3: "r", 4: "r"},
         bars=[int(r[1]) for r in rows], bar_col=2)
    timed("bloat", len(rows), started, "estimate from live/dead tuples")
    return 0


def cmd_vacuum() -> int:
    started = time.time()
    rows = rows_of("""
        select relname, n_dead_tup::text,
               coalesce(to_char(greatest(last_autovacuum, last_vacuum), 'YYYY-MM-DD HH24:MI'), 'never'),
               coalesce(to_char(greatest(last_autoanalyze, last_analyze), 'YYYY-MM-DD HH24:MI'), 'never'),
               n_mod_since_analyze::text
        from pg_stat_user_tables
        order by n_dead_tup desc, n_mod_since_analyze desc limit 20""")
    emit(["table", "dead", "last vacuum", "last analyze", "mods since analyze"],
         [[r[0], r[1], r[2], r[3], r[4]] for r in rows], align={1: "r", 4: "r"},
         bars=[float(r[1]) for r in rows], bar_col=1)
    timed("vacuum", len(rows), started)
    return 0


def cmd_seq_scans() -> int:
    started = time.time()
    rows = rows_of("""
        select relname, seq_scan::text, coalesce(idx_scan,0)::text, n_live_tup::text
        from pg_stat_user_tables where seq_scan > coalesce(idx_scan,0) and seq_scan > 10
        order by seq_scan desc limit 20""")
    emit(["table", "seq scans", "index scans", "live rows"],
         [[r[0], r[1], r[2], r[3]] for r in rows], align={1: "r", 2: "r", 3: "r"},
         bars=[float(r[1]) for r in rows], bar_col=1)
    if FMT == "table" and rows:
        note("small tables are fine to scan; big ones with a missing predicate index are not")
    timed("seq-scans", len(rows), started)
    return 0


def cmd_cache() -> int:
    started = time.time()
    hit = rows_of("""
        select round(100 * sum(blks_hit)::numeric / greatest(sum(blks_hit + blks_read), 1), 2)::text,
               sum(blks_hit)::text, sum(blks_read)::text
        from pg_stat_database where datname = current_database()""")[0]
    idx_hit = one("""
        select round(100 * sum(idx_blks_hit)::numeric / greatest(sum(idx_blks_hit + idx_blks_read),1),2)::text
        from pg_statio_user_indexes""")
    temp = rows_of("""
        select coalesce(temp_files,0)::text, pg_size_pretty(coalesce(temp_bytes,0)) from pg_stat_database
        where datname = current_database()""")[0]
    deadlocks = one("select deadlocks::text from pg_stat_database where datname = current_database()")
    emit(["metric", "value"], [
        ["buffer hit ratio", hit[0] + " %"],
        ["index hit ratio", (idx_hit or "?") + " %"],
        ["blocks hit / read", f"{int(hit[1]):,} / {int(hit[2]):,}"],
        ["temp files", temp[0]],
        ["temp bytes", temp[1]],
        ["deadlocks", deadlocks],
    ], align={1: "r"})
    timed("cache", 6, started)
    return 0


def cmd_xid() -> int:
    started = time.time()
    rows = rows_of("""
        select datname, age(datfrozenxid)::text, current_setting('autovacuum_freeze_max_age')
        from pg_database where datallowconn order by age(datfrozenxid) desc limit 5""")
    oldest = one("select coalesce(max(age(backend_xid)), 0)::text from pg_stat_activity where backend_xid is not null")
    prepared = one("select count(*)::text from pg_prepared_xacts")
    emit(["database", "xid age", "freeze max age"],
         [[r[0], f"{int(r[1]):,}", r[2]] for r in rows], align={1: "r", 2: "r"},
         bars=[float(r[1]) for r in rows], bar_col=1)
    if FMT == "table":
        note(f"oldest backend xid: {oldest} · prepared transactions: {prepared}")
        if rows and int(rows[0][1]) > int(rows[0][2]) * 0.8:
            warn_line("xid age is above 80% of autovacuum_freeze_max_age — wraparound risk, vacuum soon")
    timed("xid", len(rows), started)
    return 0


def cmd_sequences() -> int:
    started = time.time()
    rows = rows_of("""
        select schemaname || '.' || sequencename, coalesce(last_value,0)::text, max_value::text,
               round(100.0 * coalesce(last_value,0) / greatest(max_value,1), 1)::text,
               coalesce(round(100.0 * coalesce(last_value,0) / greatest(increment_by * 2147483647,1), 1), 0)::text
        from pg_sequences s join pg_class c on c.relname = s.sequencename
        order by 4 desc nulls last limit 15""")
    emit(["sequence", "last value", "max value", "of max%", "of int4%"],
         [[r[0], f"{int(r[1]):,}", f"{int(r[2]):,}", r[3] + " %", r[4] + " %"] for r in rows],
         align={1: "r", 2: "r", 3: "r", 4: "r"}, bars=[float(r[3] or 0) for r in rows], bar_col=3)
    if FMT == "table" and rows and float(rows[0][3] or 0) > 80:
        warn_line("a sequence is above 80% of its maximum — an int4 primary key will fail soon")
    timed("sequences", len(rows), started)
    return 0


def cmd_replication() -> int:
    started = time.time()
    rows = rows_of("""
        select client_addr::text, state, sync_state, coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::text,'0'),
               coalesce(extract(epoch from replay_lag)::text, '0')
        from pg_stat_replication""")
    if not rows:
        emit(["replicas"], [["none configured"]])
        timed("replication", 0, started)
        return 0
    emit(["client", "state", "sync", "behind (bytes)", "lag (s)"],
         [[r[0], r[1], r[2], f"{int(float(r[3])):,}", f"{float(r[4]):.1f}"] for r in rows], align={3: "r", 4: "r"})
    timed("replication", len(rows), started)
    return 0


def cmd_connections() -> int:
    started = time.time()
    rows = rows_of("""
        select coalesce(usename::text,'-'), coalesce(application_name,'-'), coalesce(state,'-'), count(*)::text,
               coalesce(to_char(max(now()-state_change), 'HH24:MI:SS'),'')
        from pg_stat_activity where backend_type='client backend'
        group by 1,2,3 order by 4 desc limit 20""")
    maxconn = int(one("select current_setting('max_connections')"))
    total = sum(int(r[3]) for r in rows)
    emit(["user", "application", "state", "connections", "oldest"],
         [list(r) for r in rows], align={3: "r"}, bars=[float(r[3]) for r in rows], bar_col=3)
    if FMT == "table":
        pct = 100.0 * total / max(maxconn, 1)
        note(f"{total}/{maxconn} connections in use ({pct:.0f}%)")
        if pct > 70:
            warn_line("above 70% of max_connections — a connection pool is usually the fix")
    timed("connections", len(rows), started)
    return 0


def cmd_long_running(minutes: int) -> int:
    started = time.time()
    rows = rows_of(f"""
        select pid::text, coalesce(usename::text,'-'), state,
               to_char(now()-query_start, 'HH24:MI:SS'),
               left(regexp_replace(query,'\\s+',' ','g'), 60)
        from pg_stat_activity
        where backend_type='client backend' and state <> 'idle'
          and now() - query_start > interval '{int(minutes)} minutes'
        order by query_start""")
    emit(["pid", "user", "state", "elapsed", "query"], [list(r) for r in rows])
    timed("long-running", len(rows), started, f"> {minutes} min")
    return 0


def cmd_slow(by: str, limit: int) -> int:
    started = time.time()
    if not truthy(one("select count(*) from pg_extension where extname='pg_stat_statements'")):
        print(S.dim("pg_stat_statements is not installed (CREATE EXTENSION pg_stat_statements; restart)"))
        return 1
    order = "mean_exec_time" if by == "mean" else "total_exec_time"
    rows = rows_of(f"""
        select left(regexp_replace(query,'\\s+',' ','g'), 62), calls::text,
               round(mean_exec_time::numeric,1)::text, round(total_exec_time::numeric,0)::text,
               rows::text, round(100.0*shared_blks_hit/greatest(shared_blks_hit+shared_blks_read,1),1)::text
        from pg_stat_statements order by {order} desc limit {int(limit)}""")
    emit(["query", "calls", "mean ms", "total ms", "rows", "cache%"],
         [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows],
         align={1: "r", 2: "r", 3: "r", 4: "r", 5: "r"},
         bars=[float(r[3]) for r in rows], bar_col=3,
         footer_note=f"— ranked by {by} execution time")
    timed("slow", len(rows), started)
    return 0


def cmd_grant(t: str) -> int:
    started = time.time()
    schema, name = split_table(t)
    rows = rows_of(f"""
        select grantee, string_agg(privilege_type, ', ' order by privilege_type)
        from information_schema.role_table_grants
        where table_schema={lit(schema)} and table_name={lit(name)}
        group by grantee order by grantee""")
    emit(["role", "privileges"], [list(r) for r in rows])
    timed("grants", len(rows), started, f"{schema}.{name}")
    return 0


def cmd_rls() -> int:
    started = time.time()
    rows = rows_of("""
        select c.relname, c.relrowsecurity::text,
               (select count(*)::text from pg_policies p where p.tablename=c.relname) as policies
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind='r' and n.nspname='public' order by c.relrowsecurity, c.relname""")
    gaps = [r for r in rows if not truthy(r[1])]
    emit(["table", "rls enabled", "policies"],
         [[r[0], "yes" if truthy(r[1]) else "no", r[2]] for r in rows])
    if FMT == "table" and gaps:
        note(f"{len(gaps)} table(s) without row-level security — fine for a private API, not for multi-tenant")
    timed("rls", len(rows), started)
    return 0


# ------------------------------------------------------------------ data & ops

def cmd_profile(t: str) -> int:
    schema, name = split_table(t)
    if not exists_query(schema, name):
        print(f"no such table: {schema}.{name}", file=sys.stderr)
        return 2
    started = time.time()
    rows = rows_of(f"""
        select attname, coalesce(null_frac::text,'-'), coalesce(n_distinct::text,'-'),
               coalesce(avg_width::text,'-'),
               coalesce((most_common_vals::text::text[])[1], '')
        from pg_stats where schemaname={lit(schema)} and tablename={lit(name)} order by attname""")
    out = []
    for col, nullf, distinct, width, top in rows:
        d = distinct
        if d not in ("-",) and float(d) < 0:
            d = f"~{abs(float(d)):.0%} of rows"
        out.append([col, nullf + (" %" if nullf != "-" else ""), d, width, top[:34]])
    emit(["column", "nulls", "distinct", "width", "most common"], out, align={1: "r", 2: "r", 3: "r"},
         footer_note="— from pg_stats: run ANALYZE if this looks stale")
    timed("profile", len(rows), started)
    return 0


def cmd_sample(t: str, limit: int) -> int:
    schema, name = split_table(t)
    if not exists_query(schema, name):
        print(f"no such table: {schema}.{name}", file=sys.stderr)
        return 2
    started = time.time()
    headers, rows = rows_with_header(f"select * from {schema}.{name} limit {int(limit)}")
    emit(headers, [[(c[:40] + "…") if len(c) > 40 else c for c in r] for r in rows])
    timed("sample", len(rows), started, "read-only")
    return 0


def cmd_q(sql: str) -> int:
    started = time.time()
    headers, rows = rows_with_header(sql)
    align = {i: "r" for i, h in enumerate(headers)
             if rows and all(re.fullmatch(r"-?\d+(\.\d+)?", r[i] or "0") for r in rows)}
    emit(headers, [[c[:60] for c in r] for r in rows], align=align)
    timed("query", len(rows), started)
    return 0


def cmd_explain(sql: str) -> int:
    started = time.time()
    rows = rows_of("EXPLAIN (FORMAT JSON) " + sql)
    plan = json.loads("".join(r[0] for r in rows))[0]
    root = plan["Plan"]
    warnings: list[str] = []

    def walk(node: dict, depth: int = 0) -> list[str]:
        cost = node.get("Total Cost", 0) or 0
        est = node.get("Plan Rows", 0) or 0
        label = node.get("Node Type", "?")
        detail = ""
        if node.get("Relation Name"):
            detail += f" on {node['Relation Name']}"
        if node.get("Index Name"):
            detail += f" via {node['Index Name']}"
        if node.get("Join Type"):
            detail = f" ({node['Join Type']})" + detail
        if est > 50_000 and label.endswith("Seq Scan"):
            warnings.append(f"seq scan estimating {est:,} rows on {node.get('Relation Name')} — index?")
        if (node.get("Rows Removed by Filter") or 0) > max(est, 1000):
            warnings.append(f"{node.get('Relation Name')}: filter discards {node['Rows Removed by Filter']:,} rows — predicate not indexed?")
        if str(node.get("Sort Method", "")).startswith("external"):
            warnings.append(f"sort spilled to disk ({node.get('Sort Space Used')} kB) — raise work_mem?")
        out = ["  " * depth + S.cyan(label) + detail + "  " + S.dim(f"cost {cost:,.0f} · rows {est:,}")]
        for child in node.get("Plans", []):
            out += walk(child, depth + 1)
        return out

    if FMT == "json":
        print(json.dumps({"plan": root, "warnings": warnings}, indent=2))
        return 0
    if FMT == "md":
        print("```\n" + "\n".join(esc(l) for l in walk(root)) + "\n```")
        return 0
    print(S.bold("plan") + S.dim(f"  EXPLAIN only (never executed) · total cost {root.get('Total Cost', 0):,.0f}"))
    for line in walk(root):
        print(line)
    flat: list[tuple[float, str]] = []

    def collect(node: dict, depth: int = 0) -> None:
        label = "  " * depth + node.get("Node Type", "?")
        if node.get("Relation Name"):
            label += f" on {node['Relation Name']}"
        flat.append((node.get("Total Cost", 0) or 0, label))
        for child in node.get("Plans", []):
            collect(child, depth + 1)

    collect(root)
    hottest = sorted(flat, key=lambda x: -x[0])[:5]
    top = hottest[0][0] if hottest else 1
    print()
    print(S.bold("hot paths") + S.dim("  by estimated cost"))
    for cost, label in hottest:
        print(f"  {S.green(bar(cost, top)):<18} {cost:>8,.0f}  {label}")
    for w in warnings:
        warn_line(w)
    timed("explain", 1, started)
    return 0


def cmd_locks() -> int:
    started = time.time()
    rows = rows_of("""
        select w.pid::text, coalesce(b.pid::text,''), coalesce(w.wait_event_type,''),
               left(regexp_replace(w.query,'\\s+',' ','g'), 58),
               coalesce(round(extract(epoch from (now()-w.query_start)))::text,'')
        from pg_stat_activity w
        left join lateral (select unnest(pg_blocking_pids(w.pid)) as pid) b on true
        where w.wait_event_type is not null and w.state <> 'idle'""")
    if not rows:
        print(S.green("no blocking chains — nobody is waiting on a lock"))
        timed("locks", 0, started)
        return 0
    if FMT != "table":
        emit(["pid", "blocked_by", "wait", "query", "seconds"], [list(r) for r in rows])
        return 0
    print(S.bold("blocking tree") + S.dim("  who waits on whom"))
    by: dict[str, list[list[str]]] = {}
    for pid, blocker, etype, query, waited in rows:
        by.setdefault(blocker or "?", []).append([pid, etype, waited, query])
    for blocker, waiters in by.items():
        print(f"  {S.red('pid ' + blocker)} holds the lock")
        for pid, etype, waited, query in waiters:
            print(f"    {S.yellow('└─ pid ' + pid)} waits {waited}s on {etype}  {S.dim(query)}")
    timed("locks", len(rows), started, "waiting")
    return 0


def cmd_activity() -> int:
    started = time.time()
    rows = rows_of("""
        select coalesce(state,'unknown'), count(*)::text,
               coalesce(max(round(extract(epoch from (now()-state_change)))::text),'0'),
               coalesce(max(left(regexp_replace(query,'\\s+',' ','g'),36)),'')
        from pg_stat_activity where backend_type='client backend' group by 1 order by 2 desc""")
    rows = [(r + ["", "", ""])[:4] for r in rows]
    emit(["state", "connections", "oldest s", "sample query"],
         [[r[0], r[1], r[2], r[3]] for r in rows], align={1: "r", 2: "r"},
         bars=[float(r[1]) for r in rows], bar_col=1)
    timed("activity", sum(int(r[1]) for r in rows), started)
    return 0


def cmd_sizes(limit: int) -> int:
    started = time.time()
    rows = rows_of(f"""
        select c.relname, pg_total_relation_size(c.oid)::text, pg_relation_size(c.oid)::text,
               pg_indexes_size(c.oid)::text
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind in ('r','p','m') and n.nspname not in ('pg_catalog','information_schema')
        order by pg_total_relation_size(c.oid) desc limit {int(limit)}""")
    emit(["relation", "total", "table", "indexes"],
         [[r[0], human_bytes(int(r[1])), human_bytes(int(r[2])), human_bytes(int(r[3]))] for r in rows],
         align={1: "r", 2: "r", 3: "r"}, bars=[float(r[1]) for r in rows], bar_col=1,
         footer_note=f"— {human_bytes(sum(int(r[1]) for r in rows))} total")
    timed("sizes", len(rows), started)
    return 0


def cmd_migrations() -> int:
    started = time.time()
    found = 0
    for label, sql in [
        ("alembic", "select version_num from alembic_version"),
        ("django", "select app || '.' || name as migration, applied from django_migrations order by applied desc limit 5"),
        ("prisma", "select migration_name, coalesce(finished_at::text,'pending') from _prisma_migrations order by started_at desc limit 5"),
    ]:
        try:
            headers, rows = rows_with_header(sql)
        except SystemExit:
            continue
        found += 1
        if FMT == "table":
            print(S.bold(label))
        emit(headers or [label, "value"], rows)
    if not found:
        print(S.dim("no known migration table on this database"))
        return 1
    timed("migrations", found, started, "tools detected")
    return 0


def cmd_search(pattern: str) -> int:
    started = time.time()
    rows = rows_of(f"""
        select table_schema, table_name, column_name, data_type from information_schema.columns
        where (column_name ilike {lit('%' + pattern + '%')} or table_name ilike {lit('%' + pattern + '%')})
          and table_schema not in ('pg_catalog','information_schema')
        order by table_name, ordinal_position limit 100""")
    emit(["schema", "table", "column", "type"], [list(r) for r in rows])
    timed("search", len(rows), started)
    return 0


def cmd_doctor() -> int:
    url = os.environ.get("DATABASE_URL")
    print(f"psql: {shutil.which('psql') or 'MISSING'}")
    print(f"connection: {mask(url) if url else 'libpq $PG* env vars'}")
    print(f"db name: {os.environ.get('PGDATABASE') or (urlsplit(url).path.lstrip('/') if url else '<default>')}")
    if not url and not os.environ.get("PGHOST"):
        warn_line("no DATABASE_URL and no PGHOST; psql will try a local socket")
    return 0


def cmd_audit() -> int:
    """Every health check, prioritised. The one command to run before saying 'the DB is fine'."""
    started = time.time()
    findings: list[tuple[str, str, str]] = []   # severity, title, detail

    def add(sev: str, title: str, detail: str) -> None:
        findings.append((sev, title, detail))

    try:
        for name, idx, size, scans, uniq, valid, _ in rows_of("""
                select s.relname, s.indexrelname, pg_relation_size(s.indexrelid)::text,
                       coalesce(s.idx_scan,0)::text, i.indisunique::text, i.indisvalid::text, ''
                from pg_stat_user_indexes s join pg_index i on i.indexrelid=s.indexrelid
                where i.indisvalid and not i.indisunique and s.idx_scan = 0"""):
            add("warn", f"unused index {name}.{idx}", f"{human_bytes(int(size))}, never scanned")
    except SystemExit:
        pass
    try:
        for table, con, cols, parent in rows_of("""
                select c.relname, con.conname,
                       (select string_agg(a.attname, ',') from unnest(con.conkey) k
                        join pg_attribute a on a.attrelid=c.oid and a.attnum=k), t.relname
                from pg_constraint con join pg_class c on c.oid=con.conrelid
                join pg_namespace n on n.oid=c.relnamespace join pg_class t on t.oid=con.confrelid
                where con.contype='f' and n.nspname='public'
                  and not exists (select 1 from pg_index i where i.indrelid=con.conrelid
                                  and (i.indkey::int2[])[0:array_length(con.conkey,1)-1] = con.conkey)"""):
            add("warn", f"foreign key without index: {table}({cols})", f"references {parent}; deletes scan the child")
    except SystemExit:
        pass
    try:
        for name, live, dead, size in rows_of("""
                select relname, n_live_tup::text, n_dead_tup::text, pg_total_relation_size(relid)::text
                from pg_stat_user_tables where n_dead_tup > 1000 order by n_dead_tup desc limit 5"""):
            add("info", f"dead tuples in {name}", f"{int(dead):,} dead vs {int(live):,} live — vacuum")
    except SystemExit:
        pass
    try:
        db, agev, fmax = rows_of("""
                select datname, age(datfrozenxid)::text, current_setting('autovacuum_freeze_max_age')
                from pg_database where datallowconn order by age(datfrozenxid) desc limit 1""")[0]
        if int(agev) > int(fmax) * 0.6:
            add("critical", "xid age high", f"{db}: {int(agev):,} (freeze max {fmax}) — wraparound risk")
        elif int(agev) > int(fmax) * 0.3:
            add("info", "xid age rising", f"{db}: {int(agev):,} of {fmax}")
    except (SystemExit, IndexError):
        pass
    try:
        seq = one("select round(100*sum(blks_hit)::numeric/greatest(sum(blks_hit+blks_read),1),1)::text from pg_stat_database where datname=current_database()")
        if seq and float(seq) < 95:
            add("warn", "low buffer hit ratio", f"{seq}% — cache may be too small for this working set")
    except (SystemExit, ValueError):
        pass
    try:
        for name, pct in rows_of("""
                select schemaname || '.' || sequencename,
                       round(100.0*coalesce(last_value,0)/greatest(max_value,1),1)::text
                from pg_sequences where last_value is not null"""):
            if float(pct) > 80:
                add("warn", f"sequence near exhaustion: {name}", f"{pct}% of max_value used")
    except SystemExit:
        pass
    try:
        conns, maxc = rows_of("""
                select (select count(*) from pg_stat_activity where backend_type='client backend')::text,
                       current_setting('max_connections')""")[0]
        if int(conns) > int(maxc) * 0.7:
            add("warn", "connections near limit", f"{conns}/{maxc}")
    except (SystemExit, IndexError):
        pass
    try:
        waiting = one("select count(*)::text from pg_stat_activity where wait_event_type='Lock'")
        if int(waiting):
            add("critical", "sessions blocked on locks", f"{waiting} waiting — run `pg.py locks`")
    except (SystemExit, ValueError):
        pass
    try:
        if not truthy(one("select count(*) from pg_extension where extname='pg_stat_statements'")):
            add("info", "pg_stat_statements missing", "install it to enable `slow` and query-level triage")
    except (SystemExit, ValueError):
        pass

    order = {"critical": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f[0], 3))
    if not findings:
        print(S.green("audit clean — no unused indexes, no missing FK indexes, no bloat signal, xid healthy"))
    else:
        counts = {s: sum(1 for f in findings if f[0] == s) for s in ("critical", "warn", "info")}
        print(S.bold("audit") + S.dim(f"  {counts['critical']} critical · {counts['warn']} warn · {counts['info']} info"))
        for sev, title, detail in findings:
            tag = {"critical": S.red("CRIT"), "warn": S.yellow("WARN"), "info": S.dim("INFO")}[sev]
            print(f"  {tag}  {title}")
            print(f"        {S.dim(detail)}")
    timed("audit", len(findings), started)
    return 0


# ------------------------------------------------------------------- self-test

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
            raise AssertionError(f"guard allowed: {sql!r}")
        except Refused:
            continue
    assert mask("postgres://u:p@h:5432/db") == "postgres://u:***@h:5432/db"
    assert mask("postgres://h/db") == "postgres://h/db"
    assert human_bytes(1536) == "1.5 kB" and human_bytes(0) == "0 B" and human_bytes(10**9) == "953.7 MB"
    assert bar(5, 10, 10).count("█") == 5 and bar(0, 10, 10) == ""
    assert split_table("orders") == ("public", "orders")
    assert split_table("sales.orders") == ("sales", "orders")
    assert esc("\033[32mx\033[0m") == "x"
    print("self-test ok")
    return 0


# ------------------------------------------------------------------------ main

COMMANDS = {
    "overview": (cmd_overview, []),
    "tables": (cmd_tables, ["schema", "like"]),
    "schema": (cmd_schema, ["table"]),
    "ddl": (cmd_ddl, ["table"]),
    "erd": (cmd_erd, ["schema"]),
    "fingerprint": (cmd_fingerprint, ["against"]),
    "fk": (cmd_fk, ["schema"]),
    "profile": (cmd_profile, ["table"]),
    "sample": (cmd_sample, ["table", "limit"]),
    "q": (cmd_q, ["sql"]),
    "explain": (cmd_explain, ["sql"]),
    "slow": (cmd_slow, ["by", "limit"]),
    "indexes": (cmd_indexes, ["schema"]),
    "unused-indexes": (lambda: cmd_unused_indexes(), []),
    "duplicate-indexes": (lambda: cmd_duplicate_indexes(), []),
    "missing-fk-indexes": (lambda: cmd_missing_fk_indexes(), []),
    "bloat": (lambda: cmd_bloat(), []),
    "vacuum": (lambda: cmd_vacuum(), []),
    "seq-scans": (lambda: cmd_seq_scans(), []),
    "cache": (lambda: cmd_cache(), []),
    "xid": (lambda: cmd_xid(), []),
    "sequences": (lambda: cmd_sequences(), []),
    "replication": (lambda: cmd_replication(), []),
    "connections": (lambda: cmd_connections(), []),
    "long-running": (cmd_long_running, ["minutes"]),
    "audit": (lambda: cmd_audit(), []),
    "grants": (cmd_grant, ["table"]),
    "rls": (lambda: cmd_rls(), []),
    "locks": (lambda: cmd_locks(), []),
    "activity": (lambda: cmd_activity(), []),
    "sizes": (cmd_sizes, ["limit"]),
    "migrations": (lambda: cmd_migrations(), []),
    "search": (cmd_search, ["pattern"]),
    "doctor": (lambda: cmd_doctor(), []),
}


def main(argv: list[str]) -> int:
    global FMT
    raw = argv[1:]
    for flag, name in (("--json", "json"), ("--csv", "csv"), ("--md", "md")):
        if flag in raw:
            FMT = name
            raw = [a for a in raw if a != flag]
    if "--url" in raw:
        i = raw.index("--url")
        if i + 1 < len(raw):
            os.environ["DATABASE_URL"] = raw[i + 1]
            del raw[i:i + 2]
    opts = dict(a.lstrip("-").split("=", 1) for a in raw if a.startswith("--") and "=" in a)
    args = [a for a in raw if not a.startswith("--")]
    if not args or args[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "self-test":
        return self_test()
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd} (try --help)", file=sys.stderr)
        return 2
    fn, required = COMMANDS[cmd]
    kwargs: dict = {}
    if "schema" in required:
        kwargs["schema"] = opts.get("schema", "public")
    if "like" in required:
        kwargs["like"] = opts.get("like")
    if "against" in required:
        kwargs["against"] = opts.get("against")
    if "limit" in required:
        kwargs["limit"] = int(opts.get("limit", 5 if cmd == "sample" else 15))
    if "minutes" in required:
        kwargs["minutes"] = int(opts.get("minutes", 5))
    if "by" in required:
        kwargs["by"] = opts.get("by", "total")
        if kwargs["by"] not in ("total", "mean"):
            print("--by must be total or mean", file=sys.stderr)
            return 2
    if required and required[0] in {"table", "sql", "pattern"} and not rest:
        print(f"{cmd} needs a {required[0]}", file=sys.stderr)
        return 2
    if required and required[0] in {"table", "pattern"}:
        kwargs["t" if required[0] == "table" else "pattern"] = rest[0] if required[0] == "table" else rest[0]
    if required and required[0] == "sql":
        kwargs["sql"] = " ".join(rest)
    if cmd == "q":
        kwargs = {"sql": " ".join(rest)}
    if cmd == "explain":
        kwargs = {"sql": " ".join(rest)}
    try:
        return fn(**kwargs)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
