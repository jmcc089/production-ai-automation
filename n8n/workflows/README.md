# workflows/ — every n8n workflow on the shared instance, in version control

The four workflows this instance runs, exported and normalised, plus `sync.py` to move them
in either direction and tell you when the running system has drifted from what is committed.

| file | workflow | nodes | |
|---|---|---|---|
| `holt-intake.json` | Holt & Vargas — Document Intake | 32 | active |
| `cedar-intake.json` | Cedar Healthcare — Intake Triage | 24 | active |
| `brasa-support.json` | Brasa Commerce — Support Desk | 53 | active |
| `brasa-checkout.json` | Brasa Commerce — Checkout & Order Confirmation | 11 | **archived** |

**Holt is one workflow, not two.** Until 2026-08-10 the failure recorder was separate, pointed
at by `settings.errorWorkflow`. Its three nodes — `Error Trigger` → `Classify Failure` →
`Record Failure` — now sit on the intake canvas and the pointer aims at the workflow itself,
which n8n permits and which Cedar already did. The split was what once allowed the recorder to
be inactive while reporting no problem at all.

```bash
export N8N_API_KEY=...            # an n8n public-API key
export N8N_BASE=https://n8n-production-3503.up.railway.app

python3 sync.py check             # live vs repo. Exit 1 on drift. This is the one to run.
python3 sync.py pull              # repo := live, after a change you meant to make
python3 sync.py push              # live := repo, to deploy or to restore
```

## Why this exists

On **2026-08-09** Holt's live intake workflow silently reverted to an older revision. Five nodes
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

Cedar and Brasa sat on the same instance, exposed to exactly the same accident, with nothing
watching. That is why all four are here and not only the one that was bitten.

`brasa-checkout.json` is **archived**, and n8n refuses writes to an archived workflow — `push`
says so instead of failing. `check` still reads it, which is the point: the workflow nobody
looks at is the one that breaks silently the day it is switched back on. It already nearly did:
it was still reading a Railway variable that was about to be deleted.

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

*Verified 2026-08-10: `check` was given the exact 2026-08-09 failure in the repo copy, named the
five missing nodes and exited 1; `push` round-trips byte-identical and a live intake succeeded
afterwards. The merged Error Trigger was verified by forcing a genuine unhandled failure — with
the old recorder **deactivated** — which wrote `error` / `failed` / `Deepseek API` to
`hvl_workflow_runs` from the same canvas.*

*It also caught a real drift the same day: the live canvas had been rearranged in the editor, and
`check` reported the position changes. I read them as diff noise and pushed over them, which is
the one thing this file tells you not to do. The layout was restored from backup. On layout, the
person arranging the canvas is right — the tool only tells you the two disagree, it does not know
which side should win.*
