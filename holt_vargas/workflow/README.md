# workflow/ — the two n8n workflows, in version control

`intake.json` and `failure-recorder.json` are the live workflows, exported and normalised.
`sync.py` moves them in either direction and tells you when the two have diverged.

```bash
export N8N_API_KEY=...            # an n8n public-API key
export N8N_BASE=https://n8n-production-3503.up.railway.app

python3 sync.py check             # live vs repo. Exit 1 on drift. This is the one to run.
python3 sync.py pull              # repo := live, after a change you meant to make
python3 sync.py push              # live := repo, to deploy or to restore
```

## Why this exists

On **2026-08-09** the live intake workflow silently reverted to an older revision. Five nodes
and the entire execution record vanished, and the model call narrowed from 45 s / 5 000 tokens
to 25 s / 3 000 — which, measured afterwards, was failing the *majority* of ordinary intakes.

**Nothing reported any of it.** Every response body was still correct, every email still
arrived, every database row was still written. It was found by accident, hours later, while
looking for something else. Recovery was possible only because somebody had happened to take a
backup by hand four hours earlier.

The cause was identifiable — `IF Guard Passed` had its `typeValidation` renumbered from
`version 1` to `version 3`, which an API `PUT` never does and the n8n editor does on every
save, so a canvas session carrying stale state had been saved over the top. But the reason it
went unnoticed for hours, and nearly unrecovered, is that **the workflow existed in exactly one
place and had no history**. There was no diff to read and nothing to compare against.

`check` is the answer to that, and it is worth running before any debugging session: *is the
thing I am about to debug the thing this repository describes?*

## What is committed, and what is not

The exported JSON contains the workflow's **structure and expressions**, never secrets. Nodes
reference `{{ $env.SUPABASE_SERVICE_KEY }}`, `{{ $env.DEEPSEEK_API_KEY }}` and
`{{ $env.RESEND_API_KEY }}` — the expression is stored, the value lives in Railway.

This is not left to trust. `sync.py` refuses to write a file matching a JWT, `sb_secret_`,
`sk-` or `re_` shape, and exits rather than committing one.

Volatile fields are stripped so a diff shows behaviour rather than noise: `updatedAt`,
`versionId`, `versionCounter`, `triggerCount`, `shared` and similar change on their own and
would bury the one line that matters.

## Two things to know before pushing

- **n8n's PUT accepts only `{name, nodes, connections, settings}`**, and `settings` only
  `executionOrder` and `errorWorkflow`. Sending `binaryMode` or `availableInMCP` back returns
  **400**. `sync.py push` filters them.
- **`errorWorkflow` must survive.** The Failure Recorder is a separate workflow, and an unset
  pointer means failures are recorded nowhere while reporting no problem — which is exactly how
  it was once found to be doing nothing. `push` re-reads and prints it.

## When check reports drift

**Do not patch the difference node by node.** A revert is a coherent change, and undoing half
of one is worse than undoing none: on 2026-08-09 the five missing nodes were re-added *without*
restoring the `$('Guard')` references they depend on, and the workflow rejected every valid
intake until it was rolled back.

Decide which side is right, then `pull` (live was right, record it) or `push` (repo was right,
restore it) — and drive an intake through afterwards, reading the delivered result rather than
the status code.

*Verified 2026-08-10: `check` was given the exact 2026-08-09 failure in the repo copy and
reported the five missing nodes by name and exited 1; `push` round-trips byte-identical and a
live intake succeeded afterwards.*
