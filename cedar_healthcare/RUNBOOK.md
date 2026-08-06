# Cedar Healthcare — runbook

Procedures for operating the intake triage system. Architecture is described in the project
write-up; this file is only what to type when something needs doing.

| Piece | Where | Identifier |
|---|---|---|
| Workflow | n8n on Railway | `Cedar Healthcare - Intake Triage`, id `lj0aSfymkMyVOBRZ` |
| Entry point | Railway | `POST https://n8n-production-3503.up.railway.app/webhook/cedar-intake` |
| Database | Supabase | project `mjfvjogsknnuizsoygpp`, tables prefixed `ch_` |
| Staff dashboard | Netlify | `cedarhealthcare-app` |
| Marketing site | Netlify | `cedarhealthcare-web` |
| Form | Tally | form id `2EDkgL` |

n8n is shared with two other builds. Anything that stops the n8n service stops those too.

---

## Is it working?

One command. It writes a real row, so use a `+test` address you own.

```bash
curl -s -w '\n%{http_code} in %{time_total}s\n' \
  -X POST https://n8n-production-3503.up.railway.app/webhook/cedar-intake \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"evt_probe","data":{"submissionId":"sub_probe_'"$(date +%s)"'","fields":[
      {"label":"Full name","value":"Runbook Probe"},
      {"label":"Email address","value":"you+probe@example.com"},
      {"label":"Phone number","value":"+1 512 555 0100"},
      {"label":"Tell us about your reason for visit","value":"My lower back has hurt for three weeks and it is getting worse when I sit."}]}}'
```

Healthy: `{"status":"accepted"}` and **200**, in 4–7 seconds. Then confirm the row landed:

```sql
select service_category, urgency_level, practitioner_id, source_event_id
from ch_intake_requests order by created_at desc limit 1;
```

`500` means the run died before the `Ack` node — go to *When it breaks*. A 200 with no row is not a
state this system can produce; if you see it, the `Ack` node has been rewired above the insert.

**Never reuse a `submissionId`.** A repeat is deduplicated by design: you get 200 and nothing
happens, which looks identical to a silent failure. The `$(date +%s)` above avoids this.

---

## When it breaks

Work down this list. It is ordered by how often each has actually been the cause.

**1. n8n execution list** — `n8n → Cedar Healthcare - Intake Triage → Executions`.
The failing node is highlighted and its input/output are inspectable. Full request bodies are
retained, so a lost submission can be replayed by hand. This is the single most useful place and
almost always answers it.

**2. Which node failed tells you where to go next.**

| Failing node | Usual cause | Check |
|---|---|---|
| `Deepseek API` | quota, or model id changed | Deepseek dashboard; the node has a 60s timeout |
| `Parse Deepseek Response` | model returned malformed JSON or an out-of-range urgency | the thrown message quotes the raw reply |
| `Upsert Patient` / `Insert Intake` | constraint violation or Supabase down | Supabase logs; see constraint table below |
| `Fetch Services` | `ch_services` unreachable or empty | `select * from ch_services` — must return ≥1 row |
| `Patient Confirmation` / `Notify Practitioner` | Resend rejected the send | Resend dashboard → Logs |

**3. Constraint violations** are deliberate and mean the data was wrong, not the database.

| Code | Meaning |
|---|---|
| `23514` | urgency not in 1–3, complexity or status outside its allowed set |
| `23503` | `service_category` or `role` not present in `ch_services` |
| `23505` | duplicate patient email, or a `source_event_id` already processed |
| `23502` | intake with no `patient_id` |

**4. Dashboard shows nothing** — almost always RLS, not the data. Confirm the signed-in user has a
row: `select full_name, auth_user_id from ch_practitioners where auth_user_id = '<uid>'`. Cases are
visible to their assigned practitioner and to everyone while unassigned.

**5. Netlify** — `Deploys` tab. The build substitutes `__SUPABASE_URL__` and `__SUPABASE_KEY__` from
environment variables; if the page loads but no data appears and RLS is fine, view source and check
those placeholders were actually replaced.

---

## Stop and start

**Stop intake only** (leaves the other two builds running): open the workflow in n8n and toggle it
to *Inactive*. The webhook stops accepting immediately; Tally will receive errors and retry.
Reactivating replays nothing — submissions during the window are lost unless resubmitted, so prefer
this only for short changes.

**Stop everything**: Railway → `n8n` service → *Remove*/pause. Affects Brasa and Holt & Vargas too.

**Restart n8n**: Railway → `n8n` → *Redeploy*. Workflows, credentials and history survive: they live
in the attached Postgres and the `n8n-volume`, not in the image.

**Deploy the dashboard**: push to `main`. Netlify builds automatically. To verify a deploy actually
shipped, fetch the live page and grep for the change rather than trusting the green tick.

---

## Editing the workflow safely

**Test without side effects.** Pin data on the HTTP nodes and run manually — the Code nodes execute
for real while nothing is written or emailed. This is how the contract validation in
`Parse Deepseek Response` was driven through all four failure shapes.

**Publish is a separate step.** Saving updates the draft; the running system uses the published
version. After editing, publish, then confirm `activeVersionId` equals `versionId`.

**Branch order is canvas position.** `executionOrder: v1` runs branches top to bottom by vertical
position. The `Ack` node sits below both email branches on purpose, so it executes last and the
webhook always has something to return. Moving it up will make duplicate deliveries answer 500.

---

## Adding a service

One insert. The catalog drives the classifier prompt, the validator and the foreign keys.

```sql
insert into ch_services (name, assignable, prompt_hint)
values ('Acupuncture', true, 'needle-based treatment for chronic pain, migraines and sleep problems');
```

Nothing else to change. A practitioner can then hold that `role`; until one does, intakes for it
arrive unassigned and any practitioner can claim them.

---

## Rotating a key

All three live as Railway service variables and are referenced as `{{ $env.NAME }}`:
`SUPABASE_SERVICE_KEY`, `DEEPSEEK_API_KEY`, `RESEND_API_KEY`. Change the value in Railway, redeploy,
then run the health check above. Do not put keys in the workflow — an exported workflow is not
treated as a secret.

`N8N_ENCRYPTION_KEY` is different: it decrypts stored credentials. Changing it makes every saved
credential in every workflow unreadable. Do not rotate it without exporting first.

---

## Restoring after a bad change

There is no snapshot of the workflow outside n8n's own version history. Recovery is:

1. n8n keeps prior versions — republish the last known-good one.
2. Database changes are migrations in Supabase, listed under *Database → Migrations*, and are
   reversible by writing the inverse migration. There is no automatic rollback.
3. If a submission was lost, open the failed execution, copy the webhook body, and replay it with
   the `curl` above using its original `submissionId` — the uniqueness key makes a replay safe even
   if the original partly succeeded.

**This is the weakest procedure here.** It depends on n8n's internal history and on migrations being
written carefully; there is no tested restore-from-nothing path. Treated as a known limit rather
than described as a backup strategy.

*Last verified 2026-08-06.*
