# pg-inspect

<h3 align="center">Read-only Postgres inspection that is structurally unable to write.</h3>

<p align="center">
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-3DA639?style=flat-square"></a>
  <img alt="agents" src="https://img.shields.io/badge/works%20in-pi%20%7C%20Claude%20Code%20%7C%20Codex%20%7C%20Cursor-4B5563?style=flat-square">
  <img alt="skillspector" src="https://img.shields.io/badge/SkillSpector-0%2F100%20SAFE-2EA043?style=flat-square">
  <img alt="tokens" src="https://img.shields.io/badge/session%20cost-~95%20tok-6E7681?style=flat-square">
</p>

<p align="center">
  <img alt="pg-inspect demo" src="skills/pg-inspect/assets/demo.gif" width="820">
</p>

## Install

One command for each agent — the skill is plain Markdown plus a script, so nothing is coupled to one ecosystem.

```bash
# Claude Code (plugin, stays updatable)
claude plugin marketplace add Appit-labs/appit-skills
claude plugin install pg-inspect@appit-skills

# pi
pi install git:github.com/Appit-labs/pg-inspect

# any agent that reads a skills directory
git clone git@github.com:Appit-labs/pg-inspect.git
cp -r pg-inspect/skills/pg-inspect ~/.claude/skills/      # or ~/.codex/skills/, .cursor/skills/, …
```

## Why this exists

Retyping `PGPASSWORD=… psql -h … -U … -d … -c …` is the least valuable habit in agent work, and it is one typo away from a write. This is the one-command version, safe against production.

## Use

- Shape of the data: `scripts/pg.py tables` · `schema users` · `search invoice`
- Query: `scripts/pg.py q "select status, count(*) from jobs group by 1"`
- Plan: `scripts/pg.py explain "select …"` (plans, does not execute)`
- Health: `migrations` · `locks` · `activity` · `sizes`

## Requires

- `psql` on PATH (the server can be anywhere).
- A connection: `--url`, `$DATABASE_URL`, or libpq `PGHOST/PGUSER/PGPASSWORD/PGDATABASE`.
- Python 3.9+ (stdlib only).

## What you get

- Two layers of read-only: statements are refused before connecting unless they are a single SELECT/WITH/TABLE/EXPLAIN, and psql runs with `default_transaction_read_only=on` plus a 15 s `statement_timeout`.
- Passwords are masked in every printed connection string.
- `self-test` asserts the guard against nine write statements and seven legitimate reads.

## Cost

| | tokens |
| --- | --- |
| always-on (the description, every session) | ~95 |
| on invoke | ~0.7k |

The demo above is regenerated from a real run: `demos/pg-inspect.json` holds the captured
output, `tools/make_demo.py` renders it. No invented screenshots.

## Security

Static analysis with SkillSpector: **0/100, SAFE, 0 open findings** against the committed
baseline. Every accepted finding is listed with its reason in [SECURITY.md](SECURITY.md).
The skill never uses `shell=True`, never reads or stores a credential, and never chooses a
network destination at runtime.

## Part of the Appit collection

Browser QA · CI triage · Postgres · runbooks · SSH · Trello · cloud browsers · OpenDesign · Blender —
one marketplace: [appit-skills](https://github.com/Appit-labs/appit-skills).

## License

MIT. See [LICENSE](LICENSE).
