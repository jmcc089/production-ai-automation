# Holt & Vargas — incidents

Production faults, what caused them, what was ruled out, and what was accepted in exchange for each
fix. Written after the fact from the execution record and the database, not from memory.

---

## 2026-08-08 · An intake was accepted, half written, and reported as a success

**Severity:** high — silent data loss on the main path, with the caller told it worked.
**Detected:** during an assurance pass, by reading the response body of a probe. No alert existed,
and none could have: the system had no durable execution record at the time.

### Symptom

A normal intake returned **HTTP 200 with an empty body**. Not a JSON error, not a 500 — nothing at
all. The client and the case rows existed in Supabase. The document review did not, and neither
email was sent.

So the firm had a new client and a new case in the dashboard with no review attached to them, and
the person who submitted the form received nothing and had no reason to think anything was wrong.

### Diagnosis

The n8n execution had failed at `Insert Document Review` with:

```
invalid input syntax for type integer: "0.25"
```

DeepSeek had returned `completeness_score: 0.25` — the score expressed as a **ratio** rather than as
the whole number 0–100 the prompt asked for. Postgres refused the cast, the node threw, and the run
died three nodes before `Respond to Webhook`. n8n then closed the still-open HTTP request with a
bare 200.

The 200 was the most damaging part. A 500 would have been visible to any sender; an empty 200 is
indistinguishable from success to Tally, to a curl script, and to a person watching a browser tab.

### What was ruled out

- **The database schema.** The exact failing shape was inserted directly over SQL with the workflow
  bypassed; it failed identically. The column was correct and the value was wrong, not the reverse.
- **A transport or serialisation problem in n8n.** The raw model response in the execution record
  contained the literal `0.25`. The model sent it; nothing mangled it in flight.
- **A one-off.** The same prompt was re-run and produced whole numbers most of the time. This is an
  intermittent output-shape failure, which is worse than a deterministic one — it survives casual
  testing.

**Not ruled out at the time, and it should have been:** how often this had already happened. There
was no execution record, so the historical rate is **unknowable**. Any intake lost this way before
2026-08-08 left a client and a case with no review and no trace of why.

### Decision and trade-off

Two mechanisms, deliberately overlapping, because they fail in different places.

**In the prompt:** the contract now states the score is a whole number 0–100, *"Never a fraction,
never a ratio, never a decimal."*

**In `Parse Deepseek Response`:** a coercion that converts a value in `(0,1)` to a percentage,
records the substitution as a contract violation, and falls back to a flagged `Unknown` rather than
throwing.

**In the database:** `hvl_reviews_score_range`, `hvl_reviews_label_domain` and
`hvl_reviews_confidence_domain`.

**The trade-off, stated at the time:** the coercion guesses. A model that returns `0.25` meaning
"25 %" is served correctly; one that returns `0.25` meaning something else is silently
misinterpreted. The alternative — refusing the run — loses the intake, which is what was already
happening. **A flagged guess was chosen over a clean loss**, and every coercion is recorded in
`contract_violated` so the rate is visible rather than assumed.

The database constraints are not redundant with the node. The node fixes the path that goes through
the node; the constraints fix every other write path, including the dashboard and anything written
later by hand.

### Resolution and confirmation

Verified by direct insert with the flow bypassed: `9999` refused, `-5` refused, `'Totally Fine'`
refused, `'Extremely High'` refused, a legitimate review still accepted. Then driven through the
live webhook, which now returns a JSON body on every path including failure.

### Follow-up

The absence of an execution record was the reason this was invisible, so `hvl_workflow_runs` and a
a **Failure Recorder** chain — at the time a separate workflow, folded onto the intake canvas on
2026-08-10 — were built in the same pass. A run that dies now leaves a row
stuck at `started` with its `source_event_id`, which is enough to replay it.

---

## 2026-08-08 · No client had ever received a confirmation email

**Severity:** high — every client-facing email since the build existed.
**Value of the write-up:** the diagnostic lesson, not the fix, which is one line.

### Symptom

An intake completed, the paralegal received their notification, and the client received nothing. The
execution showed the firm's email node succeeding and the client's email node returning **403** from
Resend.

### Diagnosis

The client email was still being sent from `onboarding@resend.dev`, Resend's sandbox sender, which
may only deliver to the account owner's own address. Every real client address was refused.

### What was ruled out, and how

**The obvious reading was that the daily quota was exhausted** — it was late in a day of heavy
testing across three builds, and that was the working assumption when the failure was first seen.

It was wrong, and the evidence that settled it was already in the same execution: **the firm's email
had succeeded seconds earlier from the same account.** A quota is per account, not per recipient. A
403 on one send and a 200 on another in the same run cannot be a quota.

That distinction mattered more than the fix. Treating it as a temporary provider limit would have
meant waiting for a reset that would have changed nothing, and the build would have kept never
delivering to clients.

### Resolution and confirmation

The sender was changed to the verified domain `intake@mail.mcruz-portfolio.space`. Confirmed by
driving a live intake and reading the delivered message, not the response code.

### What this changed about how the system is read

**A per-recipient failure is never a quota.** The runbook now says to check the sender domain before
the plan, because the two look identical in a status code and are opposite in cause.

---

## 2026-08-08 · A redelivery produced a second review that contradicted the first

**Severity:** medium — no loss, but two different answers existed for one submission.
**Detected:** by deliberately re-sending a submission during the assurance pass.

### Symptom

Posting the same Tally submission twice produced **two document reviews for the same case**, with
different scores and different labels. The dashboard showed whichever the query happened to order
first.

### Diagnosis

`hvl_document_reviews` had no idempotency key. The `Guard` captured `submissionId` but stored it
nowhere, so nothing downstream could tell a redelivery from a new intake. The model, being
non-deterministic, then answered the same question differently.

The case-level upsert was idempotent and masked this: the *case* was correctly not duplicated, which
made the system look idempotent to anyone checking cases rather than reviews.

### Decision and trade-off

`source_event_id` added to `hvl_document_reviews` with a **plain** `UNIQUE` constraint, plus a
`Check Existing Review` step that short-circuits to a `duplicate` response before the model is
called.

**Plain rather than partial**, deliberately: a partial unique index cannot be used by PostgREST's
`on_conflict` inference. The consequence is that the 20 pre-existing reviews with `NULL` keys are
permitted — Postgres treats NULLs as distinct — so the constraint protects everything from this
point forward and nothing before it. That is stated rather than presented as full coverage.

**The trade-off:** the short-circuit means a genuine re-submission of a corrected form is refused as
a duplicate. For an intake form where the client cannot edit and resubmit, that is the right side of
the trade; for a system where they could, it would not be.

### Confirmation

A repeat now returns, in about a second:

```json
{"success":true,"status":"duplicate",
 "message":"This submission was already processed; nothing was written and no email was sent."}
```

Verified again under load in the same pass — a retry during the concurrency-12 stage deduplicated in
1 030 ms with no write and no send.

### Follow-up

This is why the runbook warns against reusing a `submissionId` for a health check: the deduplicated
path returns 200 quickly and looks exactly like a healthy run.

---

---

## 2026-08-09 · The production workflow silently reverted to a pre-hardening revision

**Severity:** high — the execution record stopped existing, the model call was narrowed
below what ordinary intakes need, and nothing anywhere reported a problem.
**Detected:** by accident. While measuring something else, a routine query showed that none of
the day's five runs had written a `hvl_workflow_runs` row.

### Symptom

Every intake returned `200` with a correct body. The dashboard was right, the emails arrived, the
database rows were written. And `hvl_workflow_runs` had no row after `2026-08-08 20:44`.

### Diagnosis

The live workflow was a **different, older revision** than the one the documentation described.
Diffing it against the backup taken at 2026-08-08 17:55Z:

- the five `Log Run *` nodes were **gone**, and their five edges short-circuited past them
- four nodes had explicit `$('Guard').first().json.X` references replaced by `$json.X`
- the model call had narrowed from **45 000 ms / 5 000 tokens to 25 000 ms / 3 000**

**What identifies the cause:** `IF Guard Passed` had its `typeValidation` bumped from `version 1`
to `version 3`. An API `PUT` stores back whatever it is handed and never renumbers a node; the n8n
editor does exactly that on save. This was a canvas save from a stale editor session, not an API
write.

### Why it was invisible

Every one of those changes removes a safeguard without changing a success path. No response body
changes. No node errors. The one observable — a table stopping — is only observable if somebody
queries it, and nothing did.

The narrowed model call was the most damaging part and the least visible. Measured against the
**degraded** parameters, 8 runs per case:

| case | p50 | over 25 s | `finish_reason` |
|---|---|---|---|
| `h-spanish-asylum-long` | 31 982 ms | 7 / 8 | length, stop |
| `asylum-strong` | 29 709 ms | 8 / 8 | **length** |
| `very-long-description` | 28 586 ms | 7 / 8 | length, stop |
| `family-visa-typical` | 29 138 ms | 5 / 8 | length, stop |

`family-visa-typical` is an ordinary intake. The revision that had been running was failing the
majority of them, and truncating at 3 000 tokens besides.

### What was ruled out

**That this was one of the API edits made the same day.** Three nodes were changed by `PUT` on
2026-08-09; each was verified by re-reading the workflow afterwards, and each diff is exactly the
intended one. The backup taken at 00:44Z — *before* the first of those edits — already shows the
degraded revision. The loss predates every API write of that session.

### Resolution and confirmation

The first attempt was wrong and is worth recording. Re-splicing the five nodes onto the live
revision **broke the main path**: `Log Run Start` sits between `Guard` and `IF Guard Passed`, and
the live `IF Guard Passed` read `$json.guard_ok` rather than `$('Guard')…`, so with an HTTP node in
between it saw Supabase's response and rejected every valid intake. Caught within two minutes by
driving a normal intake and reading the body, rolled straight back from the backup taken minutes
earlier, and the rollback verified by another live intake.

The workflow was then rebuilt as *the 17:55Z revision plus the three node fixes made that day*,
because the `$json` de-referencing and the node loss are one change and had to be undone together.
Confirmed on the running system: 28 nodes, all five writers present, `errorWorkflow` intact, and
three live paths driven — `sent` (execution 704), `duplicate` (705, deduplicated in 559 ms) and
`rejected` (706) — each leaving its `hvl_workflow_runs` row. `Respond Rejected` also went back to
naming its reasons and its `source_event_id`, which the degraded revision returned as `null`.

### Follow-up

**The runbook could not have caught this and now can.** Its health check drives an intake and reads
the response; every observable it checked was still correct. A revision check — node count, the five
writers, the model call's timeout and ceiling — is the cheapest thing that would have failed, and it
costs no email and no model call.

**The deeper gap is that the workflow is not in version control** (assurance record, Part 1 · 14).
There is no diff to review, no history to bisect, and the only reason this was recoverable is that
a backup had been taken by hand four hours earlier. That finding stops being a documentation nicety
here: it is the reason a silent revert was possible at all.

*Every incident above was diagnosed against the running system on 2026-08-08 and confirmed by
re-driving the production webhook. Dates mean executed, not written.*
