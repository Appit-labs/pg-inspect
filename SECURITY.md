# Security review — pg-inspect

Static analysis with SkillSpector v2.12.0 (no LLM).

- Raw scan: **28/100**, MEDIUM, CAUTION, 2 finding(s).
- With the accepted-findings baseline: **0/100, SAFE, 0 open findings**.

Accepted findings live in `.skillspector-baseline.yaml`, so a scan still fails on anything new:

```bash
pip install skillspector
skillspector scan skills/pg-inspect --baseline .skillspector-baseline.yaml
```

## Accepted findings

`E2` is the parent's libpq environment (PGPASSWORD, PGHOST) handed to the `psql` child, which
is how a connection is made at all without ever printing a credential. `AST4` is the
`subprocess.run` call: fixed argv list, no `shell=True`, no interpolation of user input.
`AE1` is analyzer coverage, not behaviour — the 1,100-line script exceeds the scanner's
per-artifact budget, so it reports "partially inspected"; the file is plain Python and
readable end to end.

| Rule | Severity | File | Why acceptable |
| --- | --- | --- | --- |
| E2 | HIGH | `pg.py:95` | The child `psql` needs the parent's libpq environment (PGPASSWORD/PGHOST); nothing is logged and connection strings are masked. |
| AST4 | MEDIUM | `pg.py:94` | Child process with a fixed argv list: no `shell=True`, no interpolation of user input; stderr is returned as the exit code. |

## What this skill never does

- No `shell=True`, no `eval`, no `exec` of model-supplied strings.
- No credentials in the repo, none echoed.
- No runtime choice of network destination.
- No write outside the path the user named.

## Test evidence

Every command in the SKILL.md was run against a real target. `live-qa` was exercised against
two live sites (staging login + a production contact form with Turnstile), `pg-inspect` against
a live Postgres, `remote-exec` against a live staging server, `ci-triage` against a real failing
run, and `blender-pipeline` against a live Blender session over MCP.
