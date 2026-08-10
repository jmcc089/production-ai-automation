# Holt & Vargas — runbook

Procedures for operating the document-intake review system. Architecture is described in the
project write-up; this file is only what to type when something needs doing.

| Piece | Where | Identifier |
|---|---|---|
| Workflow | n8n on Railway | `Holt & Vargas - Document Intake`, id `xurYOv7EzKPumZq0`, 32 nodes — the only Holt automation |
| Failure recorder | same canvas | `Error Trigger` → `Classify Failure` → `Record Failure`, with `settings.errorWorkflow` pointing at this workflow **itself**. The old separate workflow `ZcVRMF8TacvQ5rQg` was folded in and deactivated on 2026-08-10 |
| Entry point | Railway | `POST https://n8n-production-3503.up.railway.app/webhook/hvl-intake` |
| Database | Supabase | project `mjfvjogsknnuizsoygpp`, tables prefixed `hvl_` |
| Model | DeepSeek | `deepseek-v4-flash`, `max_tokens` 5000, timeout 90 s, one retry, fails sideways not fatally |
| Mail | Resend | sender `intake@mail.mcruz-portfolio.space` |

**n8n is shared with Cedar Healthcare and Brasa Commerce.** Anything that stops the n8n service
stops those two as well. Restarting it is never a casual action.

---

## Is it working?

One command. It writes a real row and sends two real emails, so use a `+test` address you own.

```bash
curl -s -w '\n%{http_code} in %{time_total}s\n' \
  -X POST https://n8n-production-3503.up.railway.app/webhook/hvl-intake \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"probe_'"$(date +%s)"'","data":{"submissionId":"probe_'"$(date +%s)"'","fields":[
      {"label":"Full name","value":"Runbook Probe"},
      {"label":"Email address","value":"you+probe@example.com"},
      {"label":"Phone number","value":"+1-212-555-0100"},
      {"label":"Case type","value":["o1"],"options":[{"id":"o1","text":"Asylum"}]},
      {"label":"Document description","value":"I have my I-589 application and my personal statement. I do not have police reports."},
      {"label":"Country of origin","value":"Colombia"}]}}'
```

Healthy: `{"success":true,"message":"Intake received and processed"}` and **200**, in **10–40
seconds** — p50 is about 17 s and p95 about 37 s, measured over 38 production runs. The range is
that wide because 91 % of the wait is one model call and it is not stable. Anything under 2 s did
not call the model; read the body before assuming success.

*(The first execution of this procedure took 36.5 s, outside the 10–25 s the file originally
claimed. The range above is the measured distribution, not a guess.)*

Then confirm what actually happened — the response body is not evidence:

```sql
select source_event_id, status, stage, contract_violated, duration_ms, total_tokens, error_message
from hvl_workflow_runs order by started_at desc limit 5;
```

**Three responses that all return 200 and mean different things:**

| Body contains | Meaning |
|---|---|
| `"success":true,"message":"Intake received and processed"` | full run, row written, two emails sent |
| `"status":"duplicate"` | this `submissionId` was already processed. **Nothing was written and no email was sent.** |
| `"status":"rejected"` | the `Guard` refused it; the `reasons` array says why. No model call, no row |

**Never reuse a `submissionId` for a health check.** A repeat is deduplicated by design and returns
200 in about a second, which is indistinguishable from a healthy run to anyone reading only the
status code. The `$(date +%s)` above avoids it.

A **200 with an empty body** means the run died before the response node. This is not hypothetical —
it happened in production on 2026-08-08 and lost an intake outright; see `INCIDENTS.md`.

A **`"status":"accepted_unreviewed"`** body means no review happened. The `message` says which of
the two reasons it was — the model did not answer, or the form carried no document description at
all (in which case no model call was made and `total_tokens` is null). **The intake is safe** — client, case and a review row flagged `Unknown` were all written, the
paralegal got a `🚨 PRIORITY` mail, and the client got a neutral acknowledgement with no document
list. Nothing is lost and nothing needs replaying; a human has to read the documents. If these start
appearing in numbers, the model is slow or down — check `duration_ms` in the query above before
assuming anything about the workflow.

### Is it the *right* workflow?

Run this **first**, before the intake probe. It costs no email and no model call, and it catches the
one failure the probe above cannot: the deployed workflow silently reverting to an older revision.
On 2026-08-09 that happened — five nodes and the execution record gone, the model call narrowed —
and every observable the intake probe checks was still correct. See `INCIDENTS.md`.

```bash
export N8N_API_KEY=...
export N8N_BASE=https://n8n-production-3503.up.railway.app
python3 workflow/sync.py check
```

Healthy is two lines saying `matches repo` and exit 0. Anything else prints the nodes that differ
by name and a diff, and exits 1.

This compares the live workflow against `workflow/intake.json` and `workflow/failure-recorder.json`
in this repository, so it checks **everything** — not a handful of values somebody thought to list.
The earlier version of this section hand-checked node count, writer count, timeout, `max_tokens` and
`errorWorkflow`; it would have caught the 2026-08-09 revert and would have missed any change to a
node's code.

**When it reports drift, do not patch the difference node by node.** A revert is a coherent change
and undoing half of it is worse than undoing none: on 2026-08-09 the five missing nodes were
re-added without the `$('Guard')` references they depend on, and the workflow rejected every valid
intake until it was rolled back. Decide which side is right, then `sync.py pull` (live was right) or
`sync.py push` (repo was right), and drive an intake afterwards.


---

## When it breaks

Work down this list. It is ordered by how often each has actually been the cause.

**1 · `hvl_workflow_runs`** — the durable record, and the place to start, because it survives things
n8n does not.

```sql
select started_at, status, stage, duration_ms, error_node, error_message,
       completeness_label, contract_violated, total_tokens, source_event_id
from hvl_workflow_runs
order by started_at desc limit 20;
```

The vocabulary is deliberate and worth knowing before you read the column:

| `status` | Means | Whose fault |
|---|---|---|
| `started` | a run began and never finished — the process died mid-flight | ours |
| `ok` | completed; check `contract_violated` before trusting the classification | — |
| `ok` with `stage = 'duplicate'` | a redelivery. **No row, no email, no model call.** Counting `ok` rows over-counts real intakes unless you exclude this stage | the caller's, harmlessly |
| `rejected` | the `Guard` refused the request | the caller's |
| `blocked` | an external limit (quota, rate limit) | the provider's |
| `error` | something in our chain threw | ours |

A row stuck at `started` is the signature of a dead run. Match its `source_event_id` against the
n8n execution list.

**2 · `contract_violated = true`** — the run completed but the model's answer did not satisfy the
output contract. The intake is **preserved, not lost**: `completeness_label` reads `Unknown`, the
score is `null` rather than 0 — a score we could not read is not a score of zero — and `ai_notes`
lists exactly which fields failed. `model_call_failed = true` distinguishes "the model answered
badly" from "the model never answered". The usual cause is
`finish_reason: length` — the model spent its whole budget on reasoning. This is expected at a low
rate; a burst of it means the model is being asked something unusually hard, or `max_tokens` was
lowered.

```sql
select source_event_id, completeness_label, left(ai_notes, 200)
from hvl_document_reviews where completeness_label = 'Unknown'
order by created_at desc limit 10;
```

**3 · n8n execution list** — for anything the table cannot explain.

```bash
curl -s -H "X-N8N-API-KEY: $N8N_KEY" \
  "https://n8n-production-3503.up.railway.app/api/v1/executions?workflowId=xurYOv7EzKPumZq0&limit=10" \
  | python3 -m json.tool
```

**4 · Constraint codes.** The database refuses bad writes rather than storing them, so a failed
insert usually names its own cause. What each one is telling you:

| Constraint | What was attempted |
|---|---|
| `hvl_reviews_score_range` | a score outside 0–100, or a fraction. **Cost an intake once** — see `INCIDENTS.md` |
| `hvl_reviews_label_domain` | a label outside the four values plus `Unknown` |
| `hvl_reviews_confidence_domain` | a confidence outside High / Medium / Low |
| `hvl_cases_case_type_check` | a case type outside the firm's five |
| `hvl_cases_paralegal_must_be_paralegal` | an Attorney assigned to a paralegal slot |
| `hvl_reviews_client_must_own_case` | a review filed against a case belonging to a different client |
| `hvl_reviews_source_event_id_uniq` | a duplicate submission reached the insert |
| `hvl_clients_language_domain` | a language other than Spanish or English |
| `hvl_attorneys_specializations_domain` | a specialization that is not one of the five case types. **Silently disables routing for that person** |

**5 · Resend.** A `403` on the client email with the firm email succeeding is a *sender*
configuration problem, not a quota problem — that exact pattern meant no client received a
confirmation for the life of the build. Check the sender domain before blaming the plan.

**6 · DeepSeek.** Timeouts show as `error` with a `TimeoutError` message. The ceiling is 45 s; long
Spanish asylum narratives can exceed it. This is a known open finding, not a new fault.

---

## Stop and start

**To stop accepting intakes without touching the shared n8n service**, deactivate the workflow:

```bash
curl -s -X POST -H "X-N8N-API-KEY: $N8N_KEY" \
  "https://n8n-production-3503.up.railway.app/api/v1/workflows/xurYOv7EzKPumZq0/deactivate"
```

Reactivate with `/activate`. **Do not restart the n8n service to fix a Holt problem** — it takes
Cedar and Brasa down with it.

The webhook then returns a 404 and Tally's submission is lost, since nothing queues it. There is no
buffer in front of this system; that is a real limitation, not an oversight to be discovered later.

---

## Editing the workflow safely

**Back up first, outside the repo, every time.**

```bash
curl -s -H "X-N8N-API-KEY: $N8N_KEY" \
  "https://n8n-production-3503.up.railway.app/api/v1/workflows/xurYOv7EzKPumZq0" \
  > ~/backups/hvl-$(date -u +%Y%m%dT%H%M%SZ).json
```

Four things about the API that cost time when learned the hard way:

1. **`PUT` accepts only `name`, `nodes`, `connections`, `settings`.** Anything else returns
   `400 request/body/settings must NOT have additional properties`. Fields you omit — `active`,
   `versionId`, node-level `binaryMode` — are preserved, not deleted.
2. **`settings` accepts only `executionOrder` and `errorWorkflow`.**
3. **`executionOrder: v1` orders parallel branches by canvas position.** Chain nodes rather than
   parallelising them if order matters.
4. **`max_tokens` sent through `bodyParameters` is serialised as a string and rejected.** It must be
   in a JSON body.

**After any edit, verify from a fresh `GET`, never from what you sent**, and check the node count and
`connections` are unchanged if you only meant to edit code:

```bash
curl -s -H "X-N8N-API-KEY: $N8N_KEY" \
  ".../api/v1/workflows/xurYOv7EzKPumZq0" | python3 -c "
import json,sys; w=json.load(sys.stdin)
print('nodes', len(w['nodes']), 'active', w['active'])"
```

**If you change either Code node, re-sync the eval harness or it stops testing the deployed thing:**

```bash
# pull the live node source, then re-hash
cd holt_vargas/eval/deployed && shasum -a 256 *.js > SHA256SUMS
cd .. && python3 run.py          # must still report "deployed : matches SHA256SUMS"
```

**Then measure a band, not a run.** `python3 run.py --repeat 3`. A single green run has twice been
wrong about a change in this build.

---

## Rotating a key

The Supabase `service_role` key is a **Railway service variable**, `SUPABASE_SERVICE_KEY`, read by
10 nodes as `{{ $env.SUPABASE_SERVICE_KEY }}`.

**It leaks in failed executions.** When a Supabase node errors, n8n saves the request context and
redacts `authorization` but **not** `apikey`, which carries the same token. Confirmed on this
workflow: executions **628, 629 and 630** hold a readable `service_role` JWT. Successful runs do
not — this file previously said "every stored execution", which overstated it. See `SECURITY.md`.

To rotate:

1. Issue a **new secret key** in Supabase (`sb_secret_…`). **Do not rotate the legacy JWT secret** —
   the `anon` key is derived from it and rotating it breaks all four Netlify front ends at once.
2. Update `SUPABASE_SERVICE_KEY` in Railway. This **redeploys n8n, which takes Cedar and Brasa down
   with this build** for the duration. Never a casual action.
3. Run the health check for all three builds before disabling the old key.
4. Disable the legacy `service_role` key in Supabase.
5. **Delete executions 628, 629 and 630**, which hold copies of the old key.

Rotation buys a clean slate; it does not stop the next failed run writing the new key into an error
context. Moving the key into an n8n credential is what does that — see `SECURITY.md`.

---

## Restoring after a bad change

Restore is a manual `PUT` of a backup file. There is no automated snapshot, no version history
beyond n8n's own, and no tested restore drill.

```bash
python3 - <<'EOF'
import json, urllib.request, os
w = json.load(open(os.path.expanduser('~/backups/hvl-<timestamp>.json')))
body = {"name": w["name"], "nodes": w["nodes"],
        "connections": w["connections"], "settings": {"executionOrder": "v1"}}
r = urllib.request.Request(
    "https://n8n-production-3503.up.railway.app/api/v1/workflows/xurYOv7EzKPumZq0",
    method="PUT", data=json.dumps(body).encode(),
    headers={"X-N8N-API-KEY": os.environ["N8N_KEY"], "Content-Type": "application/json"})
print(urllib.request.urlopen(r).status)
EOF
```

**This is the weakest procedure in the file.** It restores the workflow and nothing else: not the
database, not a migration, not the dashboard. A bad migration has no documented rollback. Confirm a
restore worked by diffing node count and re-running the health check above — the `PUT` returning 200
only means n8n accepted the document.

*Every command in this file was executed against the running system on 2026-08-08. The health check
was run literally as written; see the assurance record, item 13.*
