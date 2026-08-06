# Cedar Healthcare — incidents

Written up because they were instructive, not because they were severe. Times are UTC.
Timeline claims are checkable against the Supabase migration list and the commit history.

---

## 2026-08-06 · Every patient was routed to the physiotherapist

**Severity:** high — wrong clinical ownership on every intake since the routing node was added.
**Detected:** during an assurance pass, by reading the queue rather than by any alert.

### Symptom

Every row in `ch_intake_requests` was assigned to Dr. Ana Fuentes, the physiotherapist — including
a nutrition enquiry and a sports-massage enquiry that had been classified correctly as such. The
misrouting was 100% consistent, never intermittent, and produced no errors: the workflow returned
200, the emails sent, the dashboard rendered. Nothing anywhere reported a problem.

That consistency is what made it look like configuration rather than logic. A rule that is wrong
half the time looks like a bug; a rule that is wrong every time looks like a setting.

### Diagnosis

`Assign Practitioner` read its input with `$input.first().json`.

Its upstream node, `Fetch Practitioners`, calls PostgREST, which returns a JSON **array** of three
practitioners. n8n splits a top-level array into one item per element — so the node received three
items, and `$input.first()` saw only the first of them. `.find(p => p.role === service)` was
therefore searching a one-element list and missing on two services out of three.

The miss was then hidden by a fallback:

```js
const match = list.find(p => p.role === service) || list[0];
```

`|| list[0]` supplied the first practitioner as though it were a match. The defect was not
*assignment is wrong*; it was **assignment can never fail**, which is why nothing ever surfaced it.

### What was ruled out

- **The classifier.** `service_category` was correct on every row, checked against `raw_message`
  across all 21 intakes then in the table. The model was right and the routing ignored it.
- **The practitioner records.** `select full_name, role from ch_practitioners` returned three
  distinct, correctly-spelled roles. No data fix was needed.
- **A `limit` on the fetch.** `Fetch Practitioners` had no limit, and the execution output confirmed
  all three rows arrived. The data reached the node; the node did not read it.
- **Ordering.** Considered and dismissed: reordering would have changed *which* practitioner got
  everything, not the fact that one got everything.

A database trigger was **not** ruled out — it was not considered at all. One existed
(`auto_assign_practitioner_on_intake`, 2026-07-30 15:46, sixteen minutes after the practitioner
column moved to this table) and was only discovered two weeks later during the threat-model work.
It could not have fired here, because the buggy node always supplied a non-null `practitioner_id`
and the trigger's guard is `if new.practitioner_id is null`. The conclusion was correct; the search
was not exhaustive, and it is recorded that way.

### Decision and trade-off

`$input.all()` fixed the read. That left a real question: what should happen when no practitioner
holds the classified service?

- **Throw** — the intake fails loudly, Tally retries, nothing is silently mishandled.
- **Write `null`** — the intake is stored unassigned and someone picks it up.

**Chose `null`.** A patient's request should not be rejected because of an internal staffing gap;
the message is still clinical information the clinic wants, and refusing it converts an
administrative problem into a lost patient.

**The trade-off, stated at the time:** an unassigned case can sit unseen. That is exactly what
happened next.

### The decision's consequence, twenty minutes later

Removing the fabricated assignment meant `Unknown` cases now carried `practitioner_id = null`. The
RLS policy from `cedar_rls_practitioner_scoped` (2026-08-04 16:55) read:

```sql
using (practitioner_id = (select id from ch_practitioners where auth_user_id = auth.uid()))
```

In SQL, `NULL = <uuid>` is `NULL`, not false — the row is filtered out. So every unassigned case
became invisible to **all three** practitioners simultaneously. The fix for misrouting had produced
silent disappearance, which is worse: a case routed to the wrong person is at least routed to a
person.

**This was caught by the owner reviewing the change, not by the person who wrote it.**

### Resolution

Migration `ch_intake_unassigned_visible_and_claimable` (2026-08-06 15:58):

```sql
-- read: your own cases, plus anything nobody owns
using  (practitioner_id = (select id from ch_practitioners where auth_user_id = auth.uid())
        or practitioner_id is null)
-- write: you may only ever assign a case to yourself
with check (practitioner_id = (select id from ch_practitioners where auth_user_id = auth.uid()))
```

Plus a Claim button in the dashboard (commit `740f634`, 2026-08-06 16:12).

**This resolution has its own trade-off, accepted deliberately:** all three practitioners can now
read untriaged intakes, including the patient's free-text clinical message, before anyone owns the
case. Access was widened on purpose. A three-person clinic with no receptionist has no other way for
an unclassifiable case to reach a human.

### How the fix was confirmed

1. Three probes, one per service, each reaching the correct practitioner — the first time in the
   system's history that Miguel Rivas and Carla Mendoza received a case by classification.
2. A deliberately vague submission, confirmed to land with `practitioner_id = null`,
   `needs_review = true`, and to appear with a Claim button.
3. The owner signed in as Ana and claimed it. The database showed `practitioner_id` pointing at Ana
   on **that row only** — the second unassigned case remained null, proving the update affected one
   row rather than every visible one — and the case disappeared from the other two practitioners'
   queues, which is what proves the `with check` clause does its job.

Confirming step 3 required checking that the claim was *narrow*, not merely that it worked. A policy
that let Ana claim everything she could see would have passed a looser test.

### Follow-up

The duplicate trigger was removed on 2026-08-06 (`ch_drop_duplicate_assignment_trigger`). Assignment
now has exactly one implementation. The n8n node was kept over the trigger because it is visible on
the canvas and testable with pinned data, while the trigger was invisible enough that a full read of
the workflow missed it for two weeks.

---

## 2026-08-06 · Three different faults, one indistinguishable error

**Severity:** medium — no data loss, but patients received no confirmation.
**Value of the write-up:** the diagnosability lesson, not the fixes.

### Symptom

`POST /webhook/cedar-intake` returned `500 {"message":"Error in workflow"}` — while the clinical row
was already committed and visible in the dashboard. The caller was told the request failed; the
database disagreed.

### Diagnosis

Three separate faults occurred across the day, and **all three produced that identical response**:

1. Resend's sandbox sender only delivers to the account owner's address; a test address was refused.
2. After moving to a real domain, the domain was unverified — the DNS records were correct but
   nobody had pressed **Verify DNS Records** in Resend.
3. Later, after the idempotency work, a *successful* duplicate suppression left the last node with
   zero items and n8n answered `500 "No item to return was found"`.

The first two were genuine failures. The third was correct behaviour reported as a failure.

### What was ruled out

- **The database.** Rows were present and complete every time, so nothing upstream of the insert was
  at fault. This is what localised the problem to the notification branch immediately.
- **DNS.** Checked directly with `dig` — DKIM, SPF and MX were all published correctly. That ruled
  out the propagation delay that this symptom usually indicates, and pointed at Resend's own
  verification state instead, which turned out to be `Not Started`.
- **The classifier and the contract.** Untouched in all three cases; the execution reached the email
  nodes, so everything before them had succeeded.

### Decision and trade-off

The insert is not wrapped with the notification. Making the whole run atomic — no row unless the
email sends — was rejected: losing a patient's clinical message because a mail provider is briefly
down is worse than a message arriving unannounced. **The trade-off accepted is that a row can exist
that nobody was told about**, and the dashboard cannot distinguish it from one that was notified.
`Rebecca Alvarez` is a standing example of such a row.

This is an accepted risk, not a solved problem.

### Resolution and confirmation

Sender moved to a verified domain on `mail.mcruz-portfolio.space`; the third case was fixed
separately by making the webhook's response independent of whether the email branches ran. Confirmed
by delivering to a non-owner address successfully, and by driving a duplicate submission three times
and receiving 200 with no second email each time.

### What this changed about how the system is read

A `500` from this endpoint says only "something after the trigger failed". It does not indicate
whether the patient's data was stored, and it must not be read as though it does. That is why the
runbook's health check tells you to confirm the row separately rather than trusting the status code.

---

*Last updated 2026-08-06.*
