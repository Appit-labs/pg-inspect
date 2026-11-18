#!/usr/bin/env python3
"""Read-only Postgres inspector for agents and humans. Shells out to psql; no dependencies.

    pg.py overview                      # the 10-second picture of a database
    pg.py tables [--schema public]      # tables, rows, size, with bars
    pg.py schema TABLE                  # columns, indexes, constraints, foreign keys
    pg.py ddl TABLE                     # copy-pasteable CREATE TABLE + indexes
    pg.py fk [--schema public]          # the join graph, as text
    pg.py sample TABLE [--limit 5]      # a few real rows, guarded
    pg.py q "SELECT ..."                # one read statement
    pg.py explain "SELECT ..."          # plan tree, cost bars, warnings
    pg.py slow [--limit 10]             # pg_stat_statements, worst first
    pg.py sizes [--limit 15]            # biggest relations, with bars
    pg.py locks                         # blocking tree, who waits on whom
    pg.py activity                      # what the server is doing right now
    pg.py migrations                    # alembic / django / prisma state
    pg.py doctor                        # what would connect, credentials masked
    pg.py self-test

Connection: --url postgres://... | $DATABASE_URL | libpq $PG* variables.
Safety: every statement runs in a READ ONLY transaction with a 15s timeout; anything
that is not a single read statement is refused before connecting. --json switches any
command to machine-readable output. Colour is used only on a TTY (respect NO_COLOR).
"""
from __future__ import annotations

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


class Refused(Exception):
    """A statement the tool will not run."""


# --------------------------------------------------------------------- output

class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, t: str) -> str: return self(t, "1")
    def dim(self, t: str) -> str: return self(t, "2")
    def cyan(self, t: str) -> str: return self(t, "36")
    def green(self, t: str) -> str: return self(t, "32")
    def yellow(self, t: str) -> str: return self(t, "33")
    def red(self, t: str) -> str: return self(t, "31")
    def blue(self, t: str) -> str: return self(t, "34")


def style() -> Style:
    if os.environ.get("NO_COLOR"):
        return Style(False)
    return Style(sys.stdout.isatty() and os.environ.get("TERM") != "dumb")


S = style()


def human_bytes(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def bar(value: float, biggest: float, width: int = 18) -> str:
    if biggest <= 0:
        return ""
    filled = max(1, round(width * value / biggest)) if value > 0 else 0
    return "█" * filled + "·" * (width - filled)


def parse_tsv(text: str) -> tuple[list[str], list[list[str]]]:
    lines = [ln for ln in text.splitlines() if ln != ""]
    if not lines:
        return [], []
    rows = [ln.split("\t") for ln in lines]
    return rows[0], rows[1:]


def table(headers: list[str], rows: list[list[str]], align: dict[int, str] | None = None) -> str:
    """Box-drawn table; numeric columns can be right-aligned."""
    align = align or {}
    cells = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in cells:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    def line(l: str, m: str, r: str) -> str:
        return S.dim(l + m.join("─" * (w + 2) for w in widths) + r)

    out = [line("┌", "┬", "┐")]
    out.append(S.dim("│") + "".join(
        " " + S.bold(h.ljust(widths[i] if align.get(i) != "r" else widths[i])) + " " + S.dim("│")
        for i, h in enumerate(headers)))
    out.append(line("├", "┼", "┤"))
    for r in cells:
        out.append(S.dim("│") + "".join(
            " " + (c.rjust(widths[i]) if align.get(i) == "r" else c.ljust(widths[i])) + " " + S.dim("│")
            for i, c in enumerate(r)))
    out.append(line("└", "┴", "┘"))
    return "\n".join(out)


def table_with_bars(headers: list[str], rows: list[list[str]], bar_col: int,
                    values: list[float], align: dict[int, str] | None = None) -> str:
    """Same as table(), but one column shows a bar scaled to the largest value."""
    align = align or {}
    biggest = max(values) if values else 1
    cells = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in cells:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    widths[bar_col] = 20

    def rule(l: str, m: str, r: str) -> str:
        return S.dim(l + m.join("─" * (w + 2) for w in widths) + r)

    out = [rule("┌", "┬", "┐")]
    out.append(S.dim("│") + "".join(" " + S.bold(h.ljust(widths[i])) + " " + S.dim("│") for i, h in enumerate(headers)))
    out.append(rule("├", "┼", "┤"))
    for r, v in zip(cells, values):
        r = list(r)
        r[bar_col] = S.green(bar(v, biggest)) + " " + S.dim(bar(0, 1)[:0])
        out.append(S.dim("│") + "".join(
            " " + (c.rjust(widths[i]) if align.get(i) == "r" else c.ljust(widths[i])) + " " + S.dim("│")
            for i, c in enumerate(r)))
    out.append(rule("└", "┴", "┘"))
    return "\n".join(out)


def note(msg: str) -> None:
    print(S.dim(msg))


def footer(rows: int, started: float, extra: str = "") -> None:
    ms = (time.time() - started) * 1000
    bits = [f"{rows} row{'s' if rows != 1 else ''}", f"{ms:.0f} ms"]
    if extra:
        bits.insert(0, extra)
    print(S.dim("— " + " · ".join(bits)))


# --------------------------------------------------------------------- queries

class RefusedError(Exception):
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


def which_psql() -> str:
    exe = shutil.which("psql")
    if not exe:
        raise SystemExit("psql not found on PATH: install the postgres client")
    return exe


def run_tsv(sql: str) -> list[list[str]]:
    """Run one guarded read statement; rows only, tab-separated (psql -tA)."""
    stmt = guard(sql)
    cmd = [which_psql(), "-X", "-q", "--pset=pager=off", "-v", "ON_ERROR_STOP=1",
           "-tA", "-F", "\t", "-P", "null=(null)"]
    url = os.environ.get("DATABASE_URL")
    if url:
        cmd += ["-d", url]
    cmd += ["-c", "SET default_transaction_read_only=on",
            "-c", "SET statement_timeout='15s'",
            "-c", stmt]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, "PGCONNECT_TIMEOUT": "10"})
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "psql failed")
    return [ln.split("\t") for ln in proc.stdout.splitlines() if ln != ""]


def rows_of(sql: str) -> list[list[str]]:
    return run_tsv(sql)


def run_with_header(sql: str) -> tuple[list[str], list[list[str]]]:
    """Like run_tsv but keeps psql's column headers (and drops its row-count footer)."""
    stmt = guard(sql)
    cmd = [which_psql(), "-X", "-q", "--pset=pager=off", "-v", "ON_ERROR_STOP=1",
           "-A", "-F", "\t", "-P", "null=(null)"]
    url = os.environ.get("DATABASE_URL")
    if url:
        cmd += ["-d", url]
    cmd += ["-c", "SET default_transaction_read_only=on",
            "-c", "SET statement_timeout='15s'",
            "-c", stmt]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, "PGCONNECT_TIMEOUT": "10"})
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "psql failed")
    lines = [ln for ln in proc.stdout.splitlines() if ln != "" and not re.fullmatch(r"\(\d+ rows?\)", ln)]
    if not lines:
        return [], []
    return lines[0].split("\t"), [ln.split("\t") for ln in lines[1:]]


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def split_table(table: str) -> tuple[str, str]:
    if "." in table:
        schema, _, name = table.partition(".")
        return schema or "public", name
    return "public", table


# --------------------------------------------------------------------- commands

def cmd_overview(as_json: bool) -> int:
    started = time.time()
    version = rows_of("select version()")[0][0].split(" on ")[0]
    size = rows_of("select pg_size_pretty(pg_database_size(current_database()))")[0][0]
    counts = rows_of("""
        select count(*), coalesce(sum(n_live_tup), 0)
        from pg_stat_user_tables""")[0]
    biggest = rows_of("""
        select c.relname, pg_size_pretty(pg_total_relation_size(c.oid))
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind in ('r','p','m') and n.nspname not in ('pg_catalog','information_schema')
        order by pg_total_relation_size(c.oid) desc limit 4""")
    health = rows_of("""
        select (select count(*) from pg_stat_activity where wait_event_type = 'Lock') as waiting,
               (select count(*) from pg_stat_activity where backend_type = 'client backend') as conns""")[0]
    if as_json:
        print(json.dumps({"version": version, "size": size, "tables": counts[0],
                          "rows": counts[1], "biggest": biggest, "waiting": health[0],
                          "connections": health[1]}, indent=2))
        return 0
    print(S.bold("database"), S.cyan(rows_of("select current_database()")[0][0]), S.dim("·"), version)
    print(S.bold("size    "), size, S.dim(f"· {counts[0]} tables · {int(counts[1]):,} live rows"))
    print(S.bold("busiest "), ", ".join(f"{n} {sz}" for n, sz in biggest))
    lock_txt = S.red(f"{health[0]} waiting on locks") if int(health[0]) else S.dim("no lock waits")
    print(S.bold("now     "), f"{health[1]} connections ·", lock_txt)
    footer(3, started)
    return 0


def cmd_tables(schema: str, as_json: bool) -> int:
    started = time.time()
    rows = rows_of(f"""
        select c.relname,
               coalesce(s.n_live_tup, 0)::text,
               pg_total_relation_size(c.oid)::text,
               coalesce(s.n_dead_tup, 0)::text
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        left join pg_stat_user_tables s on s.relid = c.oid
        where c.relkind in ('r','p') and n.nspname = {literal(schema)}
        order by pg_total_relation_size(c.oid) desc limit 200""")
    if as_json:
        print(json.dumps([{"table": r[0], "rows": int(r[1]), "bytes": int(r[2]), "dead": int(r[3])}
                          for r in rows], indent=2))
        return 0
    if not rows:
        print(S.dim(f"no tables in schema {schema}"))
        return 0
    display = [[r[0], f"{int(r[1]):,}", human_bytes(int(r[2]))] for r in rows]
    print(table_with_bars(["table", "rows", "size", ""], [d + [""] for d in display], 3,
                          [float(r[2]) for r in rows], align={1: "r", 2: "r"}))
    dead = sum(int(r[3]) for r in rows)
    extra = f"{schema} · {human_bytes(sum(int(r[2]) for r in rows))}"
    if dead:
        extra += f" · {dead:,} dead rows (vacuum?)"
    footer(len(rows), started, extra)
    return 0


def cmd_schema(table_name: str, as_json: bool) -> int:
    started = time.time()
    schema, name = split_table(table_name)
    cols = rows_of(f"""
        select column_name, data_type, is_nullable,
               coalesce(column_default, ''), coalesce(character_maximum_length::text, '')
        from information_schema.columns
        where table_schema = {literal(schema)} and table_name = {literal(name)}
        order by ordinal_position""")
    if not cols:
        print(f"no such table: {schema}.{name}", file=sys.stderr)
        return 2
    idx = rows_of(f"select indexname, indexdef from pg_indexes where schemaname = {literal(schema)} and tablename = {literal(name)}")
    cons = rows_of(f"""
        select con.conname, con.contype, pg_get_constraintdef(con.oid)
        from pg_constraint con
        join pg_class c on c.oid = con.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = {literal(schema)} and c.relname = {literal(name)}""")
    fks = rows_of(f"""
        select con.conname, ccu.table_name, ccu.column_name
        from pg_constraint con
        join pg_class c on c.oid = con.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        join information_schema.constraint_column_usage ccu
          on ccu.constraint_name = con.conname and ccu.constraint_schema = n.nspname
        where con.contype = 'f' and n.nspname = {literal(schema)} and c.relname = {literal(name)}""")
    if as_json:
        print(json.dumps({"table": f"{schema}.{name}",
                          "columns": [{"name": c[0], "type": c[1], "nullable": c[2] == "YES",
                                       "default": c[3]} for c in cols],
                          "indexes": [{"name": i[0], "definition": i[1]} for i in idx],
                          "constraints": [{"name": c[0], "type": c[1], "definition": c[2]} for c in cons]}, indent=2))
        return 0
    print(S.bold(f"{schema}.{name}") + S.dim(f"  {len(cols)} columns · {len(idx)} indexes"))
    body = []
    for c in cols:
        default = c[3]
        tags = []
        if c[2] == "NO":
            tags.append(S.yellow("not null"))
        if default:
            tags.append(S.dim(default[:34]))
        body.append([c[0], c[1] + (f"({c[4]})" if c[4] else ""),
                     S.yellow("not null") if c[2] == "NO" else S.dim("null"),
                     " ".join(tags[1:]) if len(tags) > 1 else ""])
    print(table(["column", "type", "null", "default"], body))
    if fks:
        print()
        print(S.bold("references"))
        for con, target, col in fks:
            print(f"  {S.cyan(con)} → {target}.{col}")
    if idx:
        print()
        print(S.bold("indexes"))
        for n, d in idx:
            hint = ""
            if "USING btree" not in d:
                hint = ""
            print(f"  {S.cyan(n)}  {S.dim(d[d.find(' USING ') + 1:][:96] if ' USING ' in d else d[:96])}{hint}")
    if cons:
        print()
        print(S.bold("constraints"))
        for n, t, d in cons:
            print(f"  {S.cyan(n)}  {S.dim(d[:96])}")
    footer(len(cols), started)
    return 0


def cmd_ddl(table_name: str) -> int:
    schema, name = split_table(table_name)
    cols = rows_of(f"""
        select column_name, data_type, is_nullable, coalesce(column_default, ''),
               coalesce(character_maximum_length::text, ''),
               coalesce(numeric_precision::text, ''), coalesce(numeric_scale::text, '')
        from information_schema.columns
        where table_schema = {literal(schema)} and table_name = {literal(name)}
        order by ordinal_position""")
    if not cols:
        print(f"no such table: {schema}.{name}", file=sys.stderr)
        return 2
    lines = [f"CREATE TABLE {schema}.{name} ("]
    defs = []
    for c, dt, nullable, default, charlen, prec, scale in cols:
        t = dt
        if charlen:
            t = f"{dt}({charlen})"
        elif prec:
            t = f"{dt}({prec}{',' + scale if scale else ''})"
        piece = f"    {c} {t}"
        if nullable == "NO":
            piece += " NOT NULL"
        if default:
            piece += f" DEFAULT {default}"
        defs.append(piece)
    cons = rows_of(f"""
        select pg_get_constraintdef(con.oid)
        from pg_constraint con join pg_class c on c.oid = con.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = {literal(schema)} and c.relname = {literal(name)} and con.contype in ('p','u','c','f')""")
    for (d,) in cons:
        defs.append(f"    {d}")
    lines.append(",\n".join(defs))
    lines.append(");")
    for n, d in rows_of(f"select indexname, indexdef from pg_indexes where schemaname = {literal(schema)} and tablename = {literal(name)}"):
        if not any(n.startswith(p) for p in ("pk_", )):
            lines.append(d.rstrip(";") + ";")
    print("\n".join(lines))
    return 0


def cmd_fk(schema: str) -> int:
    started = time.time()
    rows = rows_of(f"""
        select c.relname as child, a.attname as column,
               t.relname as parent, ta.attname as parent_column, con.conname
        from pg_constraint con
        join pg_class c on c.oid = con.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        join pg_class t on t.oid = con.confrelid
        join unnest(con.conkey, con.confkey) as k(child_att, parent_att) on true
        join pg_attribute a on a.attrelid = c.oid and a.attnum = k.child_att
        join pg_attribute ta on ta.attrelid = t.oid and ta.attnum = k.parent_att
        where con.contype = 'f' and n.nspname = {literal(schema)}
        order by 1, 2""")
    if not rows:
        print(S.dim(f"no foreign keys in {schema}"))
        return 0
    for child, col, parent, parent_col, _ in rows:
        print(f"{S.cyan(child)}.{col} {S.dim('→')} {S.cyan(parent)}.{parent_col}")
    footer(len(rows), started, schema)
    return 0


def cmd_sample(table_name: str, limit: int) -> int:
    started = time.time()
    schema, name = split_table(table_name)
    headers, rows = run_with_header(f"select * from {schema}.{name} limit {int(limit)}")
    rows = [[(c[:40] + "…") if len(c) > 40 else c for c in r] for r in rows]
    print(table(headers, rows))
    footer(len(rows), started, "read-only sample")
    return 0


def cmd_q(sql: str, as_json: bool) -> int:
    started = time.time()
    headers, rows = run_with_header(sql)
    if as_json:
        print(json.dumps([dict(zip(headers, r)) for r in rows], indent=2))
        return 0
    if not headers:
        print(S.dim("(no rows)"))
        return 0
    align = {i: "r" for i, h in enumerate(headers)
             if all(re.fullmatch(r"-?\d+(\.\d+)?", r[i] or "0") for r in rows) and rows}
    print(table(headers, [[c[:60] for c in r] for r in rows], align=align))
    footer(len(rows), started)
    return 0


def cmd_explain(sql: str, as_json: bool) -> int:
    started = time.time()
    rows = run_tsv("EXPLAIN (FORMAT JSON) " + sql)
    plan = json.loads("".join(r[0] for r in rows))[0]
    root = plan["Plan"]
    warnings: list[str] = []

    def walk(node: dict, depth: int = 0) -> list[str]:
        cost = node.get("Total Cost", 0) or 0
        actual = node.get("Actual Total Time")
        rows_est = node.get("Plan Rows", 0) or 0
        label = node.get("Node Type", "?")
        detail = ""
        if node.get("Relation Name"):
            detail += f" on {node['Relation Name']}"
        if node.get("Index Name"):
            detail += f" using {node['Index Name']}"
        if node.get("Join Type"):
            detail = f" ({node['Join Type']})" + detail
        if rows_est > 50_000 and label in {"Seq Scan", "Parallel Seq Scan"}:
            warnings.append(f"sequential scan estimating {rows_est:,} rows on {node.get('Relation Name')} — index?")
        if node.get("Rows Removed by Filter", 0) > max(rows_est, 1000):
            warnings.append(f"{node.get('Relation Name')}: filter removes {node['Rows Removed by Filter']:,} rows — predicate not indexed?")
        if node.get("Sort Method", "").startswith("external"):
            warnings.append(f"sort spilled to disk ({node.get('Sort Space Used')} kB) — raise work_mem?")
        meta = S.dim(f"cost {cost:,.0f} · rows {rows_est:,}")
        if actual is not None:
            meta = S.dim(f"{actual:.2f} ms · cost {cost:,.0f} · rows {node.get('Actual Rows', 0):,}")
        out = ["  " * depth + S.cyan(label) + detail + "  " + meta]
        for child in node.get("Plans", []):
            out += walk(child, depth + 1)
        return out

    if as_json:
        print(json.dumps({"plan": root, "warnings": warnings}, indent=2))
        return 0
    biggest = max([root.get("Total Cost", 1)] + [c.get("Total Cost", 0) for c in root.get("Plans", [])] or [1])
    print(S.bold("plan") + S.dim(f"  (EXPLAIN, not executed)  total cost {root.get('Total Cost', 0):,.0f}"))
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
        print(f"  {S.green(bar(cost, top))} {cost:>8,.0f}  {label}")
    if warnings:
        print()
        for w in warnings:
            print(S.yellow("! ") + w)
    footer(1, started)
    return 0


def cmd_slow(limit: int) -> int:
    started = time.time()
    try:
        rows = rows_of(f"""
            select left(regexp_replace(query, '\\s+', ' ', 'g'), 60) as query,
                   calls::text,
                   round(mean_exec_time::numeric, 1)::text,
                   round(total_exec_time::numeric, 0)::text,
                   rows::text
            from pg_stat_statements
            order by total_exec_time desc limit {int(limit)}""")
    except SystemExit:
        print(S.dim("pg_stat_statements is not installed — CREATE EXTENSION pg_stat_statements; and restart"))
        return 1
    top = max(float(r[3]) for r in rows) if rows else 1
    body = [[r[0], r[1], r[2] + " ms", r[3] + " ms", r[4]] for r in rows]
    print(table_with_bars(["query", "calls", "mean", "total", "rows"], body, 3,
                          [float(r[3]) for r in rows], align={1: "r", 2: "r", 4: "r"}))
    footer(len(rows), started, "by total time")
    return 0


def cmd_sizes(limit: int) -> int:
    started = time.time()
    rows = rows_of(f"""
        select c.relname, pg_total_relation_size(c.oid)::text,
               pg_relation_size(c.oid)::text,
               coalesce(pg_indexes_size(c.oid), 0)::text
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind in ('r','p','m') and n.nspname not in ('pg_catalog','information_schema')
        order by pg_total_relation_size(c.oid) desc limit {int(limit)}""")
    body = [[r[0], human_bytes(int(r[1])), human_bytes(int(r[2])), human_bytes(int(r[3]))] for r in rows]
    print(table_with_bars(["relation", "total", "table", "indexes", ""], [b + [""] for b in body], 4,
                          [float(r[1]) for r in rows], align={1: "r", 2: "r", 3: "r"}))
    footer(len(rows), started, human_bytes(sum(int(r[1]) for r in rows)))
    return 0


def cmd_locks() -> int:
    started = time.time()
    rows = rows_of("""
        select w.pid::text, coalesce(b.pid::text, ''), coalesce(w.wait_event_type, ''),
               coalesce(left(w.query, 60), ''), coalesce(round(extract(epoch from (now() - w.query_start)))::text, '')
        from pg_stat_activity w
        left join lateral (select unnest(pg_blocking_pids(w.pid)) as pid) b on true
        where w.wait_event_type is not null and w.state <> 'idle'""")
    if not rows:
        print(S.green("no blocking chains — nobody is waiting on a lock"))
        footer(0, started)
        return 0
    print(S.bold("blocking tree") + S.dim("  (who waits on whom)"))
    by_blocker: dict[str, list[list[str]]] = {}
    for pid, blocker, etype, query, waited in rows:
        by_blocker.setdefault(blocker or "?", []).append([pid, etype, waited, query])
    for blocker, waiters in by_blocker.items():
        print(f"  {S.red('pid ' + blocker)} holds the lock")
        for pid, etype, waited, query in waiters:
            print(f"    {S.yellow('└─ pid ' + pid)} waits {waited}s on {etype}  {S.dim(query)}")
    footer(len(rows), started, "waiting")
    return 0


def cmd_activity() -> int:
    started = time.time()
    rows = rows_of("""
        select coalesce(state, 'unknown'),
               count(*)::text,
               coalesce(max(round(extract(epoch from (now() - state_change)))::text), '0'),
               coalesce(max(left(regexp_replace(query, '\\s+', ' ', 'g'), 36)), '')
        from pg_stat_activity where backend_type = 'client backend'
        group by 1 order by 2 desc""")
    rows = [(r + ["", "", ""])[:4] for r in rows]
    print(table(["state", "connections", "oldest s", "sample query"],
                [[r[0], r[1], r[2], r[3]] for r in rows], align={1: "r", 2: "r"}))
    footer(sum(int(r[1]) for r in rows), started)
    return 0


def cmd_migrations() -> int:
    started = time.time()
    found = 0
    for label, sql in [
        ("alembic", "select version_num from alembic_version"),
        ("django (last 5)", "select app || '.' || name as name, applied from django_migrations order by applied desc limit 5"),
        ("prisma (last 5)", "select migration_name, coalesce(finished_at::text, '') from _prisma_migrations order by started_at desc limit 5"),
    ]:
        try:
            rows = run_tsv(sql)
        except SystemExit:
            continue
        found += 1
        print(S.bold(label))
        print(table([label, "value"], rows))
    if not found:
        print(S.dim("no known migration table on this database"))
        return 1
    footer(found, started, "migration tools detected")
    return 0


def cmd_search(pattern: str) -> int:
    started = time.time()
    rows = rows_of(f"""
        select table_schema, table_name, column_name, data_type from information_schema.columns
        where (column_name ilike {literal('%' + pattern + '%')}
           or table_name ilike {literal('%' + pattern + '%')})
          and table_schema not in ('pg_catalog', 'information_schema')
        order by table_name, ordinal_position limit 100""")
    print(table(["schema", "table", "column", "type"], rows))
    footer(len(rows), started)
    return 0


def cmd_doctor() -> int:
    url = os.environ.get("DATABASE_URL")
    print(f"psql: {shutil.which('psql') or 'MISSING'}")
    print(f"connection: {mask(url) if url else 'libpq $PG* env vars'}")
    print(f"db name: {os.environ.get('PGDATABASE') or (urlsplit(url).path.lstrip('/') if url else '<default>')}")
    if not url and not os.environ.get("PGHOST"):
        print(S.yellow("warning: no DATABASE_URL and no PGHOST; psql will try a local socket"))
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
            raise AssertionError(f"guard allowed: {sql!r}")
        except Refused:
            continue
    assert mask("postgres://u:p@h:5432/db") == "postgres://u:***@h:5432/db"
    assert mask("postgres://h/db") == "postgres://h/db"
    assert human_bytes(1536) == "1.5 kB" and human_bytes(0) == "0 B"
    assert bar(5, 10, 10).count("█") == 5 and bar(0, 10, 10).count("█") == 0
    headers, rows = parse_tsv("a\tb\n1\t2\n")
    assert headers == ["a", "b"] and rows == [["1", "2"]]
    print("self-test ok")
    return 0


def main(argv: list[str]) -> int:
    raw = argv[1:]
    as_json = "--json" in raw
    raw = [a for a in raw if a != "--json"]
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
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "overview":
        return cmd_overview(as_json)
    if cmd == "tables":
        return cmd_tables(opts.get("schema", "public"), as_json)
    if cmd == "schema" and rest:
        return cmd_schema(rest[0], as_json)
    if cmd == "ddl" and rest:
        return cmd_ddl(rest[0])
    if cmd == "fk":
        return cmd_fk(opts.get("schema", "public"))
    if cmd == "sample" and rest:
        return cmd_sample(rest[0], int(opts.get("limit", 5)))
    if cmd == "q" and rest:
        return cmd_q(" ".join(rest), as_json)
    if cmd == "explain" and rest:
        return cmd_explain(" ".join(rest), as_json)
    if cmd == "slow":
        return cmd_slow(int(opts.get("limit", 10)))
    if cmd == "sizes":
        return cmd_sizes(int(opts.get("limit", 15)))
    if cmd == "locks":
        return cmd_locks()
    if cmd == "activity":
        return cmd_activity()
    if cmd == "migrations":
        return cmd_migrations()
    if cmd == "search" and rest:
        return cmd_search(rest[0])
    print(f"unknown or incomplete command: {' '.join(args)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
