---
name: pg-inspect
description: Inspect a Postgres database read-only — list tables, show a table's columns and indexes, run a SELECT, explain a query plan, check migration state, locks, activity, and disk hogs. Use when the user asks what is in the database, why a query is slow, which tables are big, whether migrations are applied, what is blocking, or when a task needs data shape before writing SQL or code.
allowed-tools:
  - Bash
  - Read
permissions:
  - shell
  - env
  - network
license: MIT
---

# Postgres, read-only

One command per question, formatted for reading, with the guard rail that matters:
nothing here can write. Every statement runs with
`default_transaction_read_only=on` and `statement_timeout=15s`, and the script
refuses any statement that is not a single SELECT/WITH/TABLE/EXPLAIN before it
connects — so it is safe against a production URL.

```
python3 "$SKILL_DIR/scripts/pg.py" <command>
```

**Not for:** anything that writes — the script refuses non-SELECT before it connects. Designing a schema, or a slow query you cannot find, is a different job: get the SQL from your app's logs first.

## Portability

Works in any agent that can run shell commands (pi, Claude Code, Codex, Cursor,
Gemini CLI, …): the instructions are plain Markdown and the helper is a stdlib
script. Commands use `$SKILL_DIR` for this skill's own directory — the folder that
holds this `SKILL.md`. Set it once per session, for example:

```bash
SKILL_DIR=~/.claude/skills/pg-inspect     # or ~/.codex/skills/…, ~/.pi/agent/skills/…, .cursor/skills/…
```

## Commands

| Need | Command |
| --- | --- |
| the ten-second picture | `overview` |
| tables with sizes, bars, dead tuples | `tables [--schema=public] [--like=ord]` |
| columns, defaults, indexes, constraints, FKs | `schema orders` |
| copy-pasteable DDL | `ddl orders` |
| entity diagram (mermaid or terminal) | `erd` · `erd --md` |
| schema hash, and drift between two envs | `fingerprint` · `fingerprint --against postgres://staging/…` |
| the join graph | `fk` |
| per-column nulls, distinct, most-common values | `profile orders` |
| a few real rows | `sample orders --limit=5` |
| one query | `q "select status, count(*) from orders group by 1"` |
| plan tree, cost bars, warnings | `explain "select …"` |
| slowest queries by total or mean time | `slow --by=total|mean` |
| sessions stuck in a query | `long-running --minutes=5` |
| **everything below, prioritised** | **`audit`** |
| index health: size, scans, unused, duplicates, invalid | `indexes` · `unused-indexes` · `duplicate-indexes` |
| foreign keys with no supporting index | `missing-fk-indexes` |
| dead tuples and wasted bytes | `bloat` · `vacuum` |
| tables scanned more than indexed | `seq-scans` |
| hit ratios, temp spill, deadlocks | `cache` |
| transaction-ID age (wraparound) | `xid` |
| sequences near exhaustion | `sequences` |
| replicas and lag | `replication` |
| who is connected, vs max_connections | `connections` |
| privileges per table, RLS coverage | `grants orders` · `rls` |
| blocking tree / connection states | `locks` · `activity` |
| biggest relations | `sizes --limit=15` |
| migration tool state | `migrations` |
| find a table or column by name | `search invoice` |
| machine-readable | any command with `--json`, `--csv` or `--md` |

## How to use it well

- `overview` → `audit` → the specific command. `audit` is the cheapest way to find
  out whether anything is wrong before you start reading tables.
- `overview` then `schema` then `q`. Writing SQL against guessed column names is
  the most common wasted round trip in this kind of work.
- `erd --md` pastes a mermaid diagram straight into a PR or a doc; `fingerprint
  --against` tells you whether staging and prod have drifted without dumping either.
- `explain` is plan-only (no ANALYZE), so it is safe on production; read the
  `hot paths` block first, then the tree. Warnings flag seq scans on big row
  estimates, filters that discard most rows, and sorts spilling to disk.
- Output is a box table with aligned numbers and a `— rows · ms` footer. Colour is
  used only on a TTY; set `NO_COLOR=1` to force plain text, or `--json` when a
  script (not a human) is reading.
- Ask narrow questions. `q` prints raw psql output (`|` separated, no pager), so
  `select ... limit 20` beats a wide dump: the output lands in your context and
  every row costs tokens.
- `explain` runs without ANALYZE, so it plans but does not execute — the only
  plan you should ever run against production.
- Read-only means read-only: to change data, hand the user the statement or use
  their migration tool. Do not look for a write path here.

## When it refuses

- Multi-statement strings (`select 1; drop table x`) are refused by design. Send
  one statement per call.
- A real write statement is refused with the reason. That is the script working,
  not a bug to route around.
- `psql: command not found` means the Postgres client is not installed; install
  it or use `docker compose exec db psql` from the project.

## Self-check

`python3 "$SKILL_DIR/scripts/pg.py" self-test` asserts the read-only guard
against nine write statements and seven legitimate reads, and checks password
masking. Run it after editing the script, not on every use.
