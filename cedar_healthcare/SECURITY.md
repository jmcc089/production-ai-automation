# Cedar Healthcare — exposed surface

What is reachable, what refuses it, what is visible on purpose, and what is missing.
Every claim below was checked against the running system on 2026-08-06, not against the code.
Where a check is reproducible, the request is given so you can repeat it.

Cedar shares one Supabase project and one n8n instance with two other builds, isolated by table
prefix (`ch_`) and webhook path. This document covers Cedar only.

---

## 1. What is reachable without credentials

Three things are public. Nothing else is.

| Surface | Address | What it does |
|---|---|---|
| Staff dashboard | `cedarhealthcare-app.netlify.app` | Static HTML. Renders nothing until a Supabase session exists. |
| Marketing site | `cedarhealthcare-web.netlify.app` | Static. No data access. |
| Intake webhook | `POST /webhook/cedar-intake` on Railway | Accepts a Tally form payload. **No authentication.** |

The dashboard ships the Supabase **anon** key in its HTML, by design. It is substituted at build
time from a Netlify environment variable; the repository contains only the placeholder
`__SUPABASE_KEY__`. That the deployed key is the anon key and not `service_role` is verifiable —
decode the JWT payload served to any browser:

```
{"iss":"supabase","ref":"mjfvjogsknnuizsoygpp","role":"anon","iat":1779473625,...}
```

`role: anon`. Holding it grants nothing on its own, which is the next section.

---

## 2. What denies what

**Row level security, on every `ch_` table.** All policies are granted `to authenticated` only.
There is no `anon` policy anywhere, so the public key reads nothing. Checked live with that key:

| Request with the anon key | Response |
|---|---|
| `GET /rest/v1/ch_patients` | `200 []` |
| `GET /rest/v1/ch_intake_requests` | `200 []` |
| `GET /rest/v1/ch_clinical_records` | `200 []` |
| `GET /rest/v1/ch_practitioners` | `200 []` |
| `POST /rest/v1/ch_patients` | `401 · 42501 violates row-level security policy` |

An empty array rather than a 403 is PostgREST behaving correctly: the rows exist, the policy
filters them all out.

**Identity.** Practitioners sign in through Supabase Auth. `ch_practitioners.auth_user_id` maps the
auth user to the staff row, and every policy resolves the caller through it. That column carries a
unique index, because two rows sharing one `auth_user_id` would make the policies' scalar subquery
raise `21000` and the queue would error instead of narrowing.

**Constraints.** Integrity does not depend on the workflow behaving. Nine foreign keys, `CHECK`
constraints on urgency, complexity and both status columns, a service catalog referenced by every
column that names a service, and a unique index on `source_event_id` that makes a repeated webhook
delivery a no-op. Verified by attempting each violation directly against Postgres.

**Secrets.** `SUPABASE_SERVICE_KEY`, `DEEPSEEK_API_KEY` and `RESEND_API_KEY` exist only as Railway
environment variables, referenced in n8n as `{{ $env.NAME }}`. The full git history — all 32 commits,
not just the current tree — contains no key material.

---

## 3. What is deliberately visible, and why

**Any signed-in practitioner can read every patient record.** `ch_patients` and
`ch_practitioners` carry `using (true)` for authenticated users. This is a clinic of three people
sharing a front desk, and a physiotherapist who cannot look up the patient a colleague booked cannot
cover for them. Clinical detail is *not* shared this way: `ch_clinical_records` and
`ch_clinical_notes` are restricted to the practitioner they belong to.

**Untriaged intakes are visible to all three practitioners.** When the classifier cannot determine a
service, no practitioner is assigned. Scoping the queue strictly by ownership made those cases
invisible to everyone — a clinic with no receptionist would never see them. The policy therefore
reads *own or unassigned*, and a Claim button lets any practitioner take one. The `with check`
clause on the update policy allows assigning only to yourself, so claiming cannot be used to hand a
case to someone else or to take one already owned.

**Practitioner notifications all go to the owner's mailbox.** Every alert email is addressed to
`mcruzcardoza@gmail.com` regardless of who the case routed to. This is a portfolio build; the three
practitioners are fictional and their addresses are login identities, not deliverable mailboxes.
The patient-facing confirmation *is* sent to whatever address the patient supplied.

---

## 4. What is missing, and what closing it would cost

These are open. None of them are excused by this being a demo.

**The intake webhook is unauthenticated, and that is the largest hole.** Anyone who knows the URL
can post a form payload. Inspecting a real Tally delivery shows it sends **no signature header** —
the request carries only `user-agent: Tally Webhooks` and routing headers — so there is currently
nothing to verify against. Three consequences, in order of severity:

- *It sends branded mail to any address.* The patient confirmation goes to the email in the payload,
  from a verified `mail.mcruz-portfolio.space` sender. That is an open relay for
  Cedar-branded email to arbitrary recipients.
- *It costs money on demand.* Each request is one Deepseek classification. There is no rate limit.
- *It poisons the clinical queue.* Fabricated intakes are indistinguishable from real ones.

Closing it: enable Tally's signing secret if the plan offers it and verify the HMAC in a Code node
before anything else runs — an hour. If not available, a long random segment in the webhook path
plus a rate limit at the Railway edge is a weaker but same-afternoon mitigation. The relay problem
is separately closable by only sending patient confirmations to addresses already present in
`ch_patients`.

**Patient messages leave the system to two processors.** The free-text message goes to Deepseek for
classification, and name, email and message are rendered into emails sent through Resend. Both are
third parties with their own retention. No data processing agreement exists with either. Before real
clinical use this needs a signed DPA, and in most jurisdictions handling health data it needs one
before the first live patient — not as a hardening step but as a precondition.

**n8n execution history holds live credentials to patient submissions.** Tally's payload includes
`submissionPdfUrl` and `submissionPreviewUrl`, each carrying an access token and signature that
resolve to the patient's full submission. n8n stores the entire request body — the same retention
that makes lost work replayable. Anyone with access to the n8n instance therefore has direct links
to patient data, not merely a log of it. Closing it: strip both fields in the first node, and set an
execution retention window. Under an hour.

**n8n execution history stores the `service_role` key in plaintext.** Found 2026-08-06 while reading
failed executions. The Supabase HTTP nodes send the key in two headers, `apikey` and `Authorization`.
When a node errors, n8n saves the request context into the execution record and redacts
`authorization` to `**hidden**` — but **not** `apikey`, which carries the same token. Any failed
execution therefore contains a readable `service_role` JWT, and that key bypasses RLS entirely: it
reads and writes every patient, intake and clinical record in the project. At least three executions
from 2026-08-06 hold it, and n8n has no retention window configured, so they persist.

The exposure is bounded by access to the n8n instance, which is not public — but the whole point of
`service_role` is that it is the one credential that makes every other control here irrelevant.
Closing it: rotate the key in Supabase and update the Railway variable, delete the affected
executions, and set an execution retention window. Rotation is the owner's to do. Sending the key
only in `Authorization` would stop future executions from recording it, but PostgREST wants `apikey`,
so the durable fix is retention plus rotation, not header surgery.

**The practitioner alert email interpolates patient-supplied text into HTML unescaped.**
`Build Practitioner Email` places `full_name` and `raw_message` directly into the message body. Mail
clients do not execute scripts, so this is not code execution — but an attacker-supplied `<a href>`
renders as a clickable link inside a Cedar-branded email to a clinician, which is a working phishing
primitive against the exact person the system is trying to alert. The dashboard had the same defect
and it was worse there; see below. Closing it: the same escape helper, applied in the workflow node.

**Fixed 2026-08-06 — stored cross-site scripting in the dashboard.** Recorded here rather than
removed, because it was reachable by anyone with no credentials at all and it is the sharpest example
of why the unauthenticated webhook above matters. A patient name containing
`<img src=x onerror=…>`, posted to the public intake endpoint, was stored verbatim and then
interpolated into `innerHTML` by the dashboard, executing in a signed-in practitioner's browser —
the session that RLS grants clinical access to. Commit `47dfe53` applies an `esc()` helper at all 41
interpolations of database values; verified against the live site afterwards. Full chain and
verification in [ADVERSARIAL.md](ADVERSARIAL.md).

**Leaked-password protection is disabled** on Supabase Auth, so a practitioner may set a password
known to be compromised. It is a toggle in the dashboard: minutes.

**There is no audit trail.** Nothing records which practitioner read which record, or who claimed a
case. For clinical data that is usually a requirement rather than a nicety. Closing it means an
append-only table and a trigger on the paths that matter — half a day.

---

## Checking this document

```bash
ANON=<the key served at cedarhealthcare-app.netlify.app>
B=https://mjfvjogsknnuizsoygpp.supabase.co/rest/v1

# should return [] — the rows exist, the policy hides them
curl -s "$B/ch_patients?select=full_name" -H "apikey: $ANON" -H "Authorization: Bearer $ANON"

# should return 401
curl -s -X POST "$B/ch_patients" -H "apikey: $ANON" -H "Authorization: Bearer $ANON" \
     -H "Content-Type: application/json" -d '{"full_name":"x","email":"x@example.com"}'
```

If either behaves differently from what is written above, this document is wrong and should be
corrected rather than trusted.

*Last verified 2026-08-06.*
