# Holt & Vargas — exposed surface

What this build exposes today, what refuses what, and what is missing. Written to be checked
against the running configuration rather than believed.

Every claim was verified on **2026-08-08** by sending the request, decoding the key, or reading
`pg_policies` — not by reading the code. Where something is absent, this file says so without an
excuse attached; an excuse a reviewer can refute costs more trust than the gap itself.

---

## 1 · What is reachable with no credentials at all

**`POST https://n8n-production-3503.up.railway.app/webhook/hvl-intake`** — the intake endpoint.
No authentication (`authentication: NONE` on the webhook node), no rate limit, no shared secret,
no signature check. Anyone who knows the URL can submit an intake.

What that is worth to an attacker, stated concretely:

| Attack | What it costs them | What it costs us | What bounds it today |
|---|---|---|---|
| Malformed floods (`{}`, junk JSON) | one request | **nothing** — 4 nodes, ~320 ms, no model call, no write | `Guard` rejects with HTTP 400 |
| Replaying a captured submission | one request | **nothing** — 6 nodes, ~0.4 s, no model call, no send | `source_event_id` idempotency |
| Well-formed *novel* submissions | one request | one DeepSeek call (~1 200–2 400 tokens) + **two Resend sends** | **nothing** |

The third row is the real exposure and it has two parts. The cheaper part is model spend. The
worse part is **email**: the attacker chooses the client address, so they can make
`intake@mail.mcruz-portfolio.space` — a domain verified to send as this firm — deliver a
"Holt & Vargas" confirmation to a third party, and exhaust the shared Resend allowance for all
three builds while doing it.

Nothing rate-limits this. The `Guard` bounds what a *malformed* request costs; it does not bound
how many *valid-looking* ones arrive.

**Everything else requires a key.** A Supabase REST call with no `apikey` header is refused with
`401 No API key found in request` (verified against all five `hvl_` tables).

## 2 · What the public key can do

The key shipped in the deployed dashboards is the Supabase **anon** key. Confirmed to be the public
one by decoding it: `{"role":"anon","iss":"supabase","ref":"mjfvjogsknnuizsoygpp"}`.

Holding it, verified on 2026-08-08:

- `SELECT` on `hvl_clients`, `hvl_cases`, `hvl_document_reviews`, `hvl_attorneys`,
  `hvl_case_notes` → **`HTTP 200` with `[]`**. RLS is on, and no policy grants `anon`.
- `INSERT` into `hvl_clients` → refused,
  `42501 new row violates row-level security policy`.

Note the shape of that first result. RLS answers an unauthorised read with an **empty list, not a
403**. Anything monitoring this must count rows, not status codes — a 200 here means "asked and
told nothing", which looks identical to "asked and there was nothing".

## 3 · What an authenticated staff member can see, and one deliberate asymmetry

Thirteen policies across five tables. Cases, reviews and notes are scoped to the signed-in
paralegal:
`paralegal_id = (select id from hvl_attorneys where auth_user_id = auth.uid())`, for both `SELECT`
and `UPDATE`. Writes for `anon` do not exist; only `service_role` has `ALL`.

**The asymmetry, named because it is real:** `hvl_authenticated_read_clients` is `USING (true)`.
Any authenticated staff member can read **every client record** — name, email, phone, country of
origin, date of birth — including clients whose cases belong to another paralegal. The *case* is
scoped; the *person* is not.

This is intentional for a firm where reception looks up any client. It is recorded here because
"a paralegal only sees their own cases" is a true statement about cases and a false one about
client PII, and a reviewer will find the difference in thirty seconds.

## 4 · Where the credentials live, and the one that leaks

Three secrets are held as n8n environment variables and referenced as `{{ $env.NAME }}`:
`SUPABASE_SERVICE_KEY`, `RESEND_API_KEY`, `DEEPSEEK_API_KEY`. None appears in this repository —
verified by scanning the working tree **and the full git history** (`git log --all -p`) for JWT,
`sk-`, `re_` and `sb_secret` patterns. The dashboards ship `__SUPABASE_URL__` / `__SUPABASE_KEY__`
placeholders substituted at deploy time.

**The service_role key was exposed in failed executions. Closed 2026-08-11.**

The Supabase HTTP nodes sent the key in two headers, `apikey` and `Authorization`. When a node
errored, n8n saved the request context into the execution record and redacted `authorization` to
`**hidden**` — but **not** `apikey`, which carried the same token. Executions **628, 629 and 630**
(2026-08-08, `Upsert Client` failing) each hold a readable `service_role` JWT at
`resultData.error.context.request.headers.apikey`.

> **A correction to the shape of this finding, and to how it was nearly lost.** This file previously
> said the key sits in plaintext *"in every stored execution"*. That is an overstatement:
> **successful runs store no request headers at all.** The exposure was specific to *failed*
> executions of a Supabase node.
>
> It matters for a second reason. The claim was re-checked before acting on it, by scanning 30
> recent Holt executions, 12 Cedar, 12 Brasa, all workflow definitions and four backups —
> **zero JWTs** — and a correction was drafted declaring the finding false. The scan was dominated
> by successful runs, which is exactly where the key never appears. Re-run filtered to
> `status=error`, it found the leak immediately. **A scan that cannot reach the condition a finding
> describes is not evidence against it**, and a true exposure was minutes from being deleted from
> this file on the strength of one.

**The fix, and it is not rotation.** All **44** Supabase and DeepSeek nodes across the four
workflows — Holt 12, Brasa Support Desk 20, Cedar 7 (plus 2 the owner migrated by hand), Brasa
Checkout 5 — now authenticate through n8n **credentials** (`supabaseApi`, `deepSeekApi`) instead of
`{{ $env.… }}` header expressions. n8n stores credentials encrypted and redacts them from error
contexts. The `apikey` and `Authorization` headers were removed from every node; `Content-Type` and
`Prefer` stay, the latter because `resolution=merge-duplicates` is what makes the upserts idempotent.

*Verified 2026-08-11 against the exact failure condition, not a proxy.* `Upsert Client` — the same
node as execution 630 — was pointed at a non-existent table so PostgREST 404'd and it threw. In
**execution 736**:

```
apikey           **hidden**
Authorization    **hidden**
```

Both redacted, zero JWTs anywhere in the record. A DeepSeek failure was tested first and proved
nothing about this, because the leak came from Supabase nodes; the probe was redone on the right one.

**Still to do, and it is the owner's:** the key that leaked is still valid. Rotate it — issue a new
`sb_secret_…` rather than rotating the legacy JWT secret, since the `anon` key derives from that and
rotating it would break all four Netlify front ends at once — update the n8n credential, and delete
executions 628, 629 and 630, which still hold copies of the old one.

**A correction, 2026-08-11.** This file, the assurance record and a commit message all said the
same leak shape applied to `RESEND_API_KEY`, still read from the environment by 9 nodes. **It does
not.** The leak was never about `$env` — it was about *which header*. n8n redacts `authorization`
and does not redact `apikey`, and execution 630 shows both in the same request: `apikey` holding a
raw JWT, `Authorization` reading `**hidden**` two lines below it. The Resend nodes send their key
**only** as `Authorization: Bearer …`, so it was already redacted and never appeared in an error
context.

Leaving `RESEND_API_KEY` in Railway is therefore not a residual risk of this kind. A Header Auth
credential would still be tidier — one place instead of one per build — but it buys no exposure
reduction, and saying otherwise was wrong.

## 5 · What is missing, and what closing it actually costs

Listed as absent first, then the cost. No item here is excused by its cost, because for most of
them the cost is not the reason they are absent.

| Missing | Cost to close |
|---|---|
| **Authentication on the intake webhook.** Anyone with the URL can submit. | Free. n8n webhooks support Header Auth natively; Tally can send a static header. Under an hour. It is absent because nobody added it, not because it was expensive. |
| **Rate limiting.** Nothing bounds valid-looking submissions, model spend, or outbound email. | Not free at the n8n layer — needs a proxy (Cloudflare in front of the Railway domain) or a counter in Postgres checked by the `Guard`. The Postgres counter is a couple of hours and would work today. |
| **Recipient allow-listing.** The client email goes to whatever address the request supplies. | Free — a check in `Guard`. Deliberately not added: the build's purpose is to confirm receipt to real clients, and allow-listing would defeat it. The correct control is authentication on the webhook, above. |
| **Bounce and complaint handling.** A syntactically valid address at a dead domain bounces silently; the send log already contains such bounces from a sibling build. | A Resend webhook plus a suppression table. Half a day. |
| **Per-client scoping of `hvl_clients`.** See section 3. | An hour to change the policy — but it is a product decision, not an oversight. |
| **Alerting.** Failures land in `hvl_workflow_runs`; nothing watches that table. | A scheduled n8n workflow querying for `status in ('error','started')` older than a few minutes. An hour. |

## 6 · What this system holds

Real personal data belonging to real people, if used for real: full name, email, phone, country of
origin, date of birth, immigration case type, and a free-text description of somebody's
immigration situation. For asylum cases that description can contain an account of persecution.

That is the reason none of the above is filed under "it's a demo". Today the data in the tables is
seeded and test material generated by this pass — but the surface is the surface either way, and
the endpoint is live.

---

*Every claim checked against the running system on 2026-08-08. "Verified" means executed on that
date, not written on it.*
