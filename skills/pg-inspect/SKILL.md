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
| what tables exist, and which are big | `tables [--schema=public]` |
| columns, types, defaults, indexes | `schema users` |
| find a table or column by name | `search invoice` |
| one query | `q "select status, count(*) from jobs group by 1"` |
| why is this slow | `explain "select ..."` |
| are migrations applied | `migrations` |
| disk hogs | `sizes --limit=15` |
| what is blocking | `locks` |
| connection counts | `activity` |
| what would I connect to (masked) | `doctor` |

Connection comes from `--url postgres://…`, else `$DATABASE_URL`, else libpq
`PG*` variables. Passwords are masked everywhere they are printed.

## How to use it well

- `tables` and `schema` first, `q` second. Writing SQL against guessed column
  names is the most common wasted round trip in this kind of work.
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
