# Holt & Vargas — what this system must never do

The prohibitions this build is held to, and the construct that enforces each one.

This file is the statement. The database and the workflow are the enforcement. Where the two
disagree, one of them is a bug — that is the point of writing it down.

Every claim below was checked against the running system on **2026-08-08**, by attempting the
violation directly against Postgres with the normal flow bypassed, by driving the production
webhook, or by reading the node that builds each artefact. Not against the write-up. Where a
rule is enforced only by convention, the entry says so instead of being reworded until it
sounds structural.

---

## 1 · No text written by the model is ever sent to a client

The strongest guarantee in the build, and until 2026-08-08 it was false.

`Build Client Email` reads exactly two arrays, `catalogue_present` and `catalogue_missing`.
Both are produced in `Parse Deepseek Response` as `catalogue.filter(...)`, where `catalogue`
is the firm's own document list for that case type. **They are subsets of the firm's catalogue
by construction**, so no string the model composes can reach a client, whatever it returns.
The model's only influence on what a client reads is *which* known documents are marked
present — a boolean per catalogue row.

The narrative fields never go near the client: `ai_summary` and `ai_notes` appear only in
`Build Paralegal Email`.

Anything the model names that is not in the catalogue is collected into `offcatalogue`, appended
to `ai_notes`, and shown to the paralegal under *"Raised by the assistant, outside the firm
checklist (not sent to the client)"*.

*Verified 2026-08-08, execution 640.* An asylum intake mentioning a notarised pastor's letter and
a university diploma produced a client email containing exactly the ten Asylum catalogue entries,
verbatim. The pastor's letter was mapped onto the catalogue row `Witness statements if available`;
the diploma appeared only in `ai_notes` to the paralegal — *"The university diploma is not a
required asylum document."*

*What this was before.* The client email interpolated `documents_missing` straight from the model.
Execution on the same input produced the client-facing line *"Translations of non-English documents
**(if applicable)**"* — a qualification the model invented. In an immigration context that is the
firm telling a client what they must gather, in words nobody at the firm wrote.

## 2 · The model never advances the state of a case

`hvl_cases.status` defaults to `Intake Received` and `hvl_document_reviews.overall_completeness`
to `Pending Documents`. **The workflow never writes either column** — they are absent from the
JSON body of `Upsert Case` and `Insert Document Review`. `completeness_label` is a separate
descriptive column sitting beside the untouched state field.

A case therefore only leaves intake when a human moves it in the dashboard. There is no path from
a model output to a case status.

## 3 · The model's numbers and labels cannot leave their domain

Enforced twice, deliberately: `Parse Deepseek Response` coerces, and the database refuses.

| Rule | Mechanism |
|---|---|
| `completeness_score` is a whole number 0–100 | `hvl_reviews_score_range` |
| `completeness_label` ∈ four values + `Unknown` | `hvl_reviews_label_domain` |
| `confidence_level` ∈ High / Medium / Low | `hvl_reviews_confidence_domain` |

The node-level coercion alone was not enough, and this is why: on 2026-08-08 DeepSeek returned
`completeness_score: 0.25` — the score as a *ratio*. The insert failed on the integer cast and the
whole intake was lost with an empty HTTP 200 (see `INCIDENTS.md`). Coercion in the node fixes the
flow that goes through the node; the constraints fix every other write path.

*Verified 2026-08-08 by direct insert, flow bypassed:* `9999` → blocked, `-5` → blocked,
`'Totally Fine'` → blocked, `'Extremely High'` → blocked, and a legitimate review still inserts.

### 3a · The score is arithmetic, not an opinion — *added 2026-08-09*

Keeping the model's number inside a domain says nothing about whether the number is *true*. A
`completeness_score` of 100 beside one matched document is inside 0–100 and is still nonsense, and
under a prompt injection the system reported exactly that and remarked on nothing.

So `completeness_score` is no longer the model's. It is `catalogue_present / catalogue_size`, a
count over a total the system already holds, and `completeness_label` is derived from it by fixed
bands — **100 → Complete · ≥70 → Mostly Complete · ≥30 → Incomplete · else Critically Incomplete**.
Number, label and list are now one quantity in three renderings and cannot contradict each other.
The paralegal email shows the basis (`4 of 9`) so the arithmetic can be checked by hand; a client's
percentage produced by a model can be checked by nobody.

**This was not a hypothetical disagreement.** Over 93 captured responses the model's own label and
its own score already disagreed constantly: `Incomplete` spanned 30–67 while `Critically Incomplete`
spanned 0–44. Production run 697 on 2026-08-09 returned `44` labelled `Critically Incomplete`,
which the derived label corrected to `Incomplete` — an ordinary intake, no attack involved.

The model's figures are kept as `model_completeness_score` / `model_completeness_label`, never shown
to the client, and a divergence of 25 points or more raises `priority_flag` and is named in
`ai_notes`.

Two cases have **no** score rather than a low one, and this is the point rather than an edge case:

| condition | result |
|---|---|
| case type `Other`, whose catalogue is empty | `null` / `Unknown`, flagged — 0 % and 100 % are both lies about an empty checklist |
| the model's response could not be parsed at all | `null` / `Unknown` — no lists came back, so an empty present list is absence of an answer, not an answer of zero |

`hvl_reviews_score_range` and `hvl_reviews_label_domain` already permitted `NULL` and `Unknown`, so
no migration was needed. `Insert Document Review` did **not** permit it: `?? 0` turned the null into
a confident zero one node after it was computed, and the first live `Other` run
(`assurance-f02-other`) wrote `0`. Caught by reading the row, not the response. The same expression
also carried `confidence_level ?? ''`, which `hvl_reviews_confidence_domain` would reject — an
insert that throws is the empty-200 incident exactly. Both now coalesce to `null`;
`assurance-f02-other2` writes `NULL` / `Unknown`.

## 4 · A case's paralegal is a Paralegal, and its attorney an Attorney

`hvl_cases.paralegal_id` and `attorney_id` are plain foreign keys to `hvl_attorneys`, which on its
own permits assigning a case to anyone in the table regardless of role. Enforced declaratively
instead: `hvl_cases` carries two constant generated columns, `paralegal_role` and `attorney_role`,
and composite foreign keys reference `hvl_attorneys(id, role)`.

*Verified 2026-08-08:* assigning an Attorney's id to `paralegal_id` is refused by
`hvl_cases_paralegal_must_be_paralegal`. Before the constraint existed the same insert succeeded.

**Limit, stated rather than glossed:** the constraint pins the *role*, not `active`. A paralegal
deactivated after assignment keeps their open cases. Reassignment on deactivation is a workflow
concern and is not implemented.

## 5 · A case never exists without a client, and a client is never duplicated

`hvl_cases_client_id_fkey` and `hvl_clients_email_key`. *Verified 2026-08-08:* a case pointing at a
random UUID is refused by the foreign key; a second client with an existing email is refused by the
unique constraint. `hvl_cases_client_case_unique` on `(client_id, case_type)` means one client has
at most one case per type, which is what makes the intake upsert idempotent at the case level.

## 6 · A case type outside the firm's five never enters the system

`hvl_cases_case_type_check` allows `Family Visa`, `Employment-Based Green Card`, `Asylum`,
`Naturalization`, `Other`. The `Guard` node independently maps anything else to `Other` and records
the substitution, so a bad value is corrected at the door rather than turned into a 400 three nodes
later. *Verified 2026-08-08:* inserting `'Divorce'` directly is refused by the check constraint.

## 7 · An intake is never accepted without a usable email and an idempotency key

`Guard` rejects the request with HTTP 400 and a reason list before any model call or write.
*Verified 2026-08-08:* six malformed requests were refused in ~320 ms having run four nodes, with
no DeepSeek call and no row written.

**Limit:** the email check is syntax and length only. It stops empty and malformed addresses; it
does not prove the mailbox exists. A well-formed address at a dead domain still bounces at Resend.

## 8 · Client and case data are never readable without authentication

RLS is enabled on all five `hvl_` tables, with 13 policies. Every policy grants to `authenticated`
or `service_role`; none grants to `anon`.

*Verified 2026-08-08 with the project's anon key:* `SELECT` on all five tables returns
**HTTP 200 with `[]`** — allowed to ask, nothing visible — and an anonymous `INSERT` into
`hvl_clients` is refused with `42501 new row violates row-level security policy`.

Note the shape of that result: RLS answers an unauthorised read with an empty list, not a 403.
Anything checking these tables must count rows, not status codes.

## 9 · A paralegal sees only their own cases — with one deliberate exception

`hvl_cases`, `hvl_document_reviews` and `hvl_case_notes` are scoped by
`paralegal_id = (select id from hvl_attorneys where auth_user_id = auth.uid())`, for both `SELECT`
and `UPDATE`.

**The exception, named because it is real:** `hvl_authenticated_read_clients` has `USING (true)`.
Any authenticated staff member can read **every client record** — full name, email, phone, country
of origin, date of birth — including clients whose cases belong to another paralegal. The case is
scoped; the person is not. This is a deliberate asymmetry for a firm where reception needs to look
up any client, not an oversight, but it means "a paralegal only sees their own cases" is a
statement about *cases*, not about *client PII*.

## 10 · Model output is escaped everywhere it is rendered

Both email builders and the paralegal dashboard escape `& < > " '` on every interpolated value.
The dashboard's `esc()` is applied to all model- and client-derived strings, including inside
attribute contexts such as `data-doc="…"`.

*Verified 2026-08-08:* a client named `<img src=x onerror=alert(1)> Vargas "test" & co` was
delivered in the confirmation email as
`&lt;img src=x onerror=alert(1)&gt; Vargas &quot;test&quot; &amp; co`.

**Limit:** the email *subject* carries the name unescaped. Subjects are plain text, not markup, so
this renders harmlessly — but it is why the subject of that test email reads literally
`🚨 PRIORITY — New H&V Intake: <img src=x onerror=alert(1)> …`.

## 11 · A review is never filed against the wrong client

`hvl_document_reviews` names both a case and a client. Until 2026-08-08 each had its own foreign
key and nothing tied them together, so a review could point at the right case and the wrong person —
and the dashboard renders the client from `client_id` while building the checklist from `case_id`,
so the screen would have shown one client's name above another client's documents.

Enforced by `hvl_reviews_client_must_own_case`, a composite foreign key on
`(case_id, client_id)` referencing `hvl_cases(id, client_id)`. Both columns are NOT NULL, so it is
always checked. *Verified 2026-08-08:* the mis-attributed insert is refused; a correct one still
succeeds.

## 12 · Two fields that route or address a person cannot hold arbitrary values

`hvl_clients_language_domain` restricts `preferred_language` to Spanish or English, and
`hvl_attorneys_specializations_domain` restricts `specializations` to the five case types.

The second matters more than it looks. `hvl_assign_case_staff` routes with
`new.case_type = any(a.specializations)`, so a value that is not a case type can never match: a
typo silently switches off specialisation routing for that person, the trigger leaves the slot
NULL, and the case falls through to whoever the workflow picked first — with no error raised
anywhere. *Verified 2026-08-08:* `'Klingon'` and `specializations = {Divorce}` are both refused.

## 13 · Staff are deactivated, never deleted

All nine foreign keys are `ON DELETE NO ACTION`, deliberately. *Verified 2026-08-08:* deleting a
client who still has cases, or an attorney still assigned to cases, is refused by the database.
`hvl_document_reviews.reviewed_by` is the same, which means an attorney who has signed off a review
can never be deleted — correct for an audit trail, and the reason departure is an
`active = false` operation rather than a delete.

---

## What is not enforced, and is known

- **A document is only ticked if the client's own words carry an anchor for it.** *Closed
  2026-08-09.* Rule 1 guarantees the client only ever reads names from the firm's catalogue; it said
  nothing about *which* of them are ticked, and that was a boolean the model alone decided.
  *Measured 2026-08-08:* given "My previous lawyer handled everything for us last year, so I assume
  the whole package was submitted properly", the production prompt marked **all nine Family Visa
  documents present** in 1 run of 3 — a client told their file was complete on the strength of a
  stated assumption. A prompt clause blocked it 3-of-3 but cost 12–15 availability failures per 57
  runs against a control's 2, and was reverted (assurance record, item 08).

  What ships instead is deterministic and costs no tokens: `Parse Deepseek Response` reads
  `document_description` and only shows a claimed document as present if the client's text carries
  its **form number** or a term for it in either language the firm accepts. Anchors only ever
  *remove* a claim, never add one, so the failure moves to the recoverable side — at worst a client
  is asked to confirm something they already sent. What the model claimed but the client never said
  is kept as `catalogue_inferred`, raises `priority_flag`, and is named to the paralegal in
  `ai_notes`.

  **The residual, stated:** the anchor table is a fixed vocabulary. A client who writes *"les mandé
  todo lo que pidieron"* without naming anything anchors nothing and is asked for everything. The
  table is built from the catalogue and the frozen eval set only; the holdout was used to measure,
  not to tune.

- **`norm()` cut every accented Spanish word in half, and had since it was written.** `[^a-z0-9]`
  turned `declaración` into `declaraci n`, so no Spanish term containing a tilde could match
  anything — in `same()` as well as in the anchoring. Found on 2026-08-09 because the holdout case
  `h-spanish-asylum-long` regressed and the cause was not the new code. Diacritics are now stripped
  via `NFD` before the class is applied. Roughly half the firm's intake is written in Spanish.
- **A client is asked for documents that do not apply to them.** `catalogue_missing` is
  `catalogue.filter(not present)` with no exception, and `Build Client Email` renders it verbatim,
  so a conditional row such as `I-751 if applicable` is demanded of every client who does not
  affirmatively claim it. `documents_optional` exists in the model contract and currently only
  feeds `offcatalogue`; it is the natural home for a third state and is not used as one.
- **Document matching is loose in one direction.** `same()` compares on substring both ways, so a
  bare claim of `"passport"` marks `Beneficiary passport` present. Deliberately left alone during
  the item-08 regression pair rather than changed inside a measurement.
- **There is no third state between "has it" and "does not have it", and the model keeps producing
  one.** *Measured 2026-08-09.* Given *"I have the I-130 receipt notice"*, the model puts
  `Petition I-130` in **missing** 3 runs of 3 and explains itself every time: *"Client has an I-130
  receipt notice rather than the Petition I-130 itself; confirm if the actual petition is
  available."* That is a correct legal distinction and there is nowhere to record it, so it survives
  only as prose in `ai_notes` and the client is asked for a document they may effectively have. The
  same gap swallows conditional rows such as `I-751 if applicable`. `documents_optional` exists in
  the model contract and currently only feeds `offcatalogue`; it is the natural home for this state.
  **Closing it needs the firm's rules first — which related documents satisfy which rows — not code
  first.**

  *Decided 2026-08-11: an I-130 receipt notice does **not** satisfy `Petition I-130`.* The current
  behaviour is therefore correct and nothing changes: the client is asked for the petition and the
  paralegal is told, in `ai_notes`, that a receipt notice was mentioned. The reasoning is the one
  this whole build runs on — asking for a document the client may already have is recoverable;
  accepting one they do not have is not. The holdout case `h-family-visa-petitioner-resident` still
  fails on `present_includes: ["Petition I-130"]`, because that expectation encodes the opposite
  judgement, written before anyone asked. It is left failing and labelled rather than edited to
  match: **a holdout that is corrected to agree with the system stops measuring it.**

  The third state itself stays open, for conditional rows such as `I-751 if applicable`, which are
  still demanded of every client who does not affirmatively claim them.
- **No authentication on the intake webhook, until a variable is set.** The `Guard` gained a
  shared-secret check on 2026-08-11 — a hidden Tally field compared against
  `$env.HVL_INTAKE_SECRET`, rejected in ~70 ms before the model — but it is **inert while the
  variable is unset**, and says so in `guard_notes` on every run rather than leaving it to be
  assumed. Until the owner sets it and adds the Tally field, anyone who knows the URL can submit an
  intake: the `Guard` bounds the damage — a malformed request costs four nodes and no model call —
  but a *well-formed* request still costs a DeepSeek call and writes a row. **Nothing rate-limits
  this even once the secret is on.**
- **`active` is not enforced at assignment time** (see 4).
- **Deliverability is not verified** (see 7).
- **No mechanism stops a paralegal reading another paralegal's client** (see 9).

---

*Checked against the running system on 2026-08-08. Every "verified" line above means executed on
that date, not written on it.*
