# Brasa Commerce — exposed surface

What is reachable, what stops what, what is visible on purpose, and what is missing. Every claim
below was checked against the running system on 2026-08-07 — by firing the endpoint, by decoding the
key the site actually serves, or by reading `pg_policy` — not by reading the code that is supposed to
implement it. Where a claim could not be driven, it says so.

This is a portfolio build handling invented customers and invented orders. That lowers the
consequence of everything here; it does not change what the surface is, and this document describes
the surface rather than the consequence.

The last section is a command you can run to check the important claims yourself.

---

## 1. What is reachable without credentials

**Three n8n webhooks, all unauthenticated.** No shared secret, no signature, no origin check, no rate
limit. Anyone with the URL can call any of them, and the URLs are in the page source of both public
sites.

| Endpoint | What a call does |
|---|---|
| `POST /webhook/brasa-intake` | one DeepSeek completion, writes a customer and a ticket, sends up to two emails |
| `POST /webhook/brasa-checkout` | prices a cart server-side, writes an order, its line items and a shipment, sends one email |
| `POST /webhook/brasa-approve` | claims a ticket by id and emails that ticket's customer |

**Two Netlify sites**, both public and both serving the Supabase **anon** key in their HTML. That the
served key is the anon key and not `service_role` is verifiable rather than asserted — decode the
JWT's payload and read the `role` claim. Done on 2026-08-07 for both sites: `"role":"anon"`.

**Supabase PostgREST**, at `mjfvjogsknnuizsoygpp.supabase.co`, with that anon key.

The repository holds no keys. Both apps ship `__SUPABASE_URL__` / `__SUPABASE_KEY__` placeholders
substituted at deploy time, and a search of the **full git history** — not just the current tree —
found no committed JWT, Resend key or DeepSeek key, and no tracked `.env`.

## 2. What denies what

**Row Level Security is enabled on all eight `bc_` tables.** With the site's own published anon key:

| Table | anon | Verified |
|---|---|---|
| `bc_product_catalog` | SELECT where `active = true` | 200, rows returned |
| `bc_support_tickets` | nothing | 200 `[]` |
| `bc_customers` | nothing | 200 `[]` |
| `bc_orders` | nothing | 200 `[]` |
| `bc_shipments` | nothing | 200 `[]` |
| `bc_order_items` | nothing | 200 `[]` |
| `bc_workflow_runs` | nothing | 200 `[]` |
| `bc_agent` | nothing | 200 `[]` |

Writes as anon: `INSERT` into `bc_support_tickets` is refused with `42501 new row violates
row-level security policy`. `UPDATE` is not refused — it **matches zero rows and returns
`204 No Content`**, which is byte-identical to a successful update. Nothing changed (checked by
counting ticket statuses before and after), but a caller cannot tell "denied" from "done" on this
path. That is PostgREST's behaviour, not a policy gap, and it is recorded because the same shape
caused a real defect elsewhere in this build.

**A signed-in agent can read every table and update every ticket.** The policies are
`using (true) with check (true)` for `authenticated` — there is no per-agent scoping. Deliberate, and
called out again in §4.

**`service_role` bypasses RLS entirely** and is used by n8n only. It is not in either app.

**SECURITY DEFINER functions: there are three, and this section named only one until 2026-08-08.**
That omission is recorded rather than quietly corrected, because the enumeration was the point of this
document and it was done from the functions the *workflow calls* instead of the ones the *database
exposes*.

| function | runs past RLS | who may call it | why |
|---|---|---|---|
| `search_order(query_text, customer_email)` | yes | `service_role` only | the workflow resolves a customer's own order |
| `bc_track_order(p_order_number)` | yes | **`anon`** — deliberate | it *is* the public order-tracking page; see §3 |
| `bc_sync_customer_ticket_count()` | yes | `service_role` only | a trigger function; nothing calls it directly |

`bc_place_order(jsonb)` was added 2026-08-08 and is **deliberately not** on that list: it is a plain
`SECURITY INVOKER` function, because the only caller already holds the service role. A function that
writes orders has no business running with definer rights it does not need.

`bc_sync_customer_ticket_count` held the default `PUBLIC` grant until 2026-08-08 and was therefore
reachable by `anon`. Nothing legitimate calls a trigger function directly, and called directly it
errors rather than doing damage — but it is `SECURITY DEFINER` and there was no reason for an
anonymous caller to reach it. Revoked; verified from outside with the site's own key, which now gets
`PGRST202` instead of an invocation, while the ticket counters stay consistent.

`search_order(query_text, customer_email)` runs past RLS by design so
the workflow can resolve orders. Until 2026-08-07 it was **callable with the public anon key** and
returned real order data — shipping city, state, postal code, tracking number — to anyone supplying a
matching order number and customer email. The function had been created with `REVOKE EXECUTE … FROM
anon, authenticated`, which did nothing, because Postgres grants `EXECUTE` to `PUBLIC` by default and
both roles are members of `PUBLIC`. Found by calling it from outside with the site's own key. Now
revoked from `PUBLIC` and granted to `service_role`; re-checked from outside, it answers
`42501 permission denied for function search_order`.

**The approval endpoint no longer chooses its own recipient.** Until 2026-08-07 `brasa-approve` read
`customer_email` from the request body and mailed it — an open relay out of a verified sending
domain. Confirmed by run, not by reading: execution `387` built
`to = mcruzcardoza+relay-probe@gmail.com` for a ticket whose customer is a different address. The
recipient now comes from the claimed ticket's own customer row; re-driven with the identical request,
execution `389` built the customer's real address instead.

Two things still gate that endpoint, both from the idempotency work: the caller must know a real
ticket's UUID, and the ticket must be `draft` or `escalated`, so each id works at most once.

## 3. What is deliberately visible, and why

- **The active product catalog, with prices.** It is a storefront; the catalog is the product.
  Inactive rows are excluded by the policy predicate itself, not by the query the app happens to
  send — verified: `?active=eq.false` returns `[]` as anon.
- **The anon key, in the page source of both sites.** That is what an anon key is for. Its power is
  exactly the table above.
- **An order number alone reveals the whole order, including the customer's name and street
  address.** This is the single most consequential deliberate exposure in the build and it deserves
  more than the one line it used to get. `bc_track_order(p_order_number)` is `SECURITY DEFINER`,
  callable with the public anon key, takes **no email and no other credential**, and returns
  `ship_name`, `ship_line1`, `ship_line2`, `ship_city`, `ship_state`, `ship_postal_code`,
  `ship_country`, every line item with prices, the totals, the shipment status and the tracking
  number. Verified from outside with the key the site serves:

  ```
  BC-8J3HBBBX → Rafael Brandt · 1420 W Belmont Ave, Apt 3R · Chicago, IL 60657, US
                2 items · $198.00 · paid · preparing
  ```

  **It is a product decision, not an oversight, and the storefront says so to the customer** in the
  tracking view itself: *"Anyone with this order number can see these details. Treat it like a
  receipt — we ask for it instead of a password so you never need an account."* Every field the
  function returns is rendered on that page; trimming the function would break it.

  The order number is the credential. Eight characters from a 31-character alphabet is about
  8.5 × 10¹¹ possibilities, so guessing one is not the risk — **forwarding one is**. A confirmation
  email shared with anyone, or a screenshot, hands over the address. The mitigations that would
  change this are an account system or a second factor such as the order email, and neither is built.

  This exposure and the `search_order` lockdown in §2 look contradictory and are not: `search_order`
  feeds the AI reply path, where the sender's address is known and must be enforced, so scoping it is
  free. `bc_track_order` feeds a page whose entire premise is that no account is required.
- **Order numbers in URLs.** The storefront tracks an order at `#track/BC-XXXXXXXX`, with
  `UNIQUE(order_number)` behind them.
- **Everything in the database is invented.** No real customer, no real address, no real payment
  instrument. There is no card processing anywhere in this build; the checkout says so on the form.

## 4. What is missing, and what closing it would cost

Stated without excuses. Where something is cheap and still not done, that is the honest position.

**The webhooks are unauthenticated, and that is the largest gap.** A shared-secret header checked in
an early node is roughly an hour's work including redeploying the two callers. It is not done. The
consequence today is cost and noise rather than data theft:

- **Trigger cost.** Each intake call spends one DeepSeek completion and up to two Resend emails. This
  is not hypothetical — this pass exhausted the Resend account's **daily send quota** on 2026-08-07
  with a few dozen legitimate probes, which is the same thing an attacker would do on purpose. The
  Resend account is **shared with two other builds**, so exhausting it takes all three down.
- **State poisoning.** Anyone can create tickets, customers and real orders with chosen shipping
  addresses. Prices cannot be manipulated — they are resolved server-side against the catalog and the
  request's price fields are never read — but the queue and the order table can be filled.

**No rate limiting anywhere.** n8n on Railway applies none and none is configured. Closing this
properly needs a proxy in front; it is not a config toggle, and calling it cheap would be wrong.

**The `service_role` key is stored in plaintext in n8n's execution history.** Confirmed for Brasa by
run: execution `381` contains a readable `service_role` JWT in the saved request. n8n redacts the
`authorization` header but not `apikey`, and Supabase requires the same token in both. That key
bypasses every policy in §2, so it is the one credential that makes the rest of this document
irrelevant. Rotating it is the owner's action, and the interim mitigation — deleting error executions
and shortening the retention window — conflicts with keeping this assurance pass's evidence, so it
belongs at the end of the pass rather than now. **This is shared with Cedar and Holt: one n8n
instance, one key, three projects.**

**No per-agent scoping on tickets.** Any signed-in agent reads and updates any ticket. For a
single-agent demo this costs nothing today; the policy predicate to scope it is a few lines, and the
reason it is not written is that there is no second agent to scope against, not that it is expensive.
Cedar made the opposite choice for clinical data and paid for it in a separate incident.

**No CSP, no `X-Frame-Options`, no `Referrer-Policy` on either Netlify site.** `Strict-Transport-
Security` is present. Adding the rest is a `[[headers]]` block in `netlify.toml` — fifteen minutes.
Not done. It matters less than it looked: at item 09 every interpolation in both front ends was
extracted and classified, and each one carrying customer-controlled text is wrapped in an `esc()`
helper — including the one inside a single-quoted `onclick`, which is why the storefront's helper
escaping `'` is load-bearing. Headers are still worth adding; they are no longer the last line of
defence.

**~~A checkout interrupted between its writes leaves a partial order.~~ Closed 2026-08-08.** Order,
line items and shipment were three separate HTTP calls from n8n, so a run that died between them left
a real paid order with no shipment — and the retry, being idempotent on `source_event_id`, returned
that broken order rather than repairing it. Confirmed by run: `BC-6ZHJ2SSD`, `paid`, one line item, no
shipment.

They are now one call to `bc_place_order(jsonb)`, a plpgsql function and therefore one transaction:
all three rows or none. It also refuses to write an order with no line items, which is a check none of
the three separate writes could make because each one only knew about itself.

**Verified three ways.** A failing call left **zero** orders behind, where the old chain left one. A
live checkout wrote order + 2 items + 1 shipment and answered in 1.6 s. The same idempotency key sent
again returned the same order number with `status: duplicate` and wrote nothing. `BC-6ZHJ2SSD` is now
the only partial order in the table and is kept as the evidence for this entry.

It is **not** `SECURITY DEFINER` — n8n calls it with the service role, which already bypasses RLS, so
definer rights would buy nothing and would add a fourth entry to the table in §2. `EXECUTE` is granted
to `service_role` alone.

**Reply content is caller-supplied.** `brasa-approve` takes `final_reply` from the request and puts it
in the email. Since the recipient fix, it can only reach the ticket's own customer. **The escaping
half was adjudicated at assurance item 09 and closed**: it was a real hole — a message carrying
`<script>` arrived raw in the team email, confirmed by reading the HTML the system built — and all
three email builders now escape every customer-controlled interpolation. Escaping fixes injection,
not content: a plausible-looking reply can still say anything inside Brasa's branding.

**Both public webhooks now reject malformed input instead of dying silently.** Until 2026-08-07 an
intake with no email address wrote a customer row keyed on `''` and auto-sent; a body that was not
the Tally shape, or carried a NUL byte, returned `HTTP 200` with an empty body after failing three
nodes later; and there was no size cap anywhere — one request stored 1.1 MB and bought a model call.
`Guard Intake` and `Checkout Valid?` now answer `400` with the reason. The full table of what was
driven is `ADVERSARIAL.md`.

**A junk message no longer buys a model call.** `Has Substance?` routes a message with fewer than ten
letters or digits past `DeepSeek API` to a canned classification, so it is written and queued to a
human without a completion being spent. Verified by execution data: `DeepSeek API` unexecuted on an
empty message and on an emoji-only message, 1 015 tokens on a real question. Together with the
4 000-character cap this removes both halves of the trigger-cost amplification — **but not the reason
it existed**, which is that the endpoint has no authentication.

**The cost of a single call is now bounded on both sides.** The input was already capped at 4 000
characters; the output was not capped at all, and item 11 measured a single classification consuming
4 322 output tokens over 35 seconds. `max_tokens` is now 2 000. This matters here and not only in the
cost item: an unauthenticated endpoint whose per-call cost has no ceiling is a different exposure from
one whose per-call cost is bounded, because the attacker chooses the input that maximises it.

---

## Checking this document

Nothing here needs to be taken on trust. `ANON` is the key in either site's page source.

```bash
SB=https://mjfvjogsknnuizsoygpp.supabase.co/rest/v1
SITE=https://brasacommerce-web.netlify.app
ANON=$(curl -s $SITE | grep -o 'eyJ[A-Za-z0-9_-]\{10,\}\.[A-Za-z0-9_-]\{10,\}\.[A-Za-z0-9_-]\{10,\}' | head -1)
q() { curl -s -H "apikey: $ANON" -H "Authorization: Bearer $ANON" "$@"; echo; }

# the role claim in the key the site actually serves — expect "role":"anon"
p=$(echo "$ANON" | cut -d. -f2); printf '%s%s\n' "$p" "$(printf '=%.0s' $(seq 1 $(( (4 - ${#p} % 4) % 4 ))))" | tr '_-' '/+' | base64 -d

q "$SB/bc_support_tickets?select=id&limit=1"          # rows exist, policy hides them  -> []
q "$SB/bc_orders?select=order_number&limit=1"         #                                -> []
q "$SB/bc_product_catalog?select=sku,price&limit=2"   # meant to be readable           -> rows
q "$SB/bc_product_catalog?select=sku&active=eq.false" # excluded by policy, not query  -> []

# the AI path's order lookup is no longer public -> 42501 permission denied for function
q -X POST "$SB/rpc/search_order" -H 'Content-Type: application/json' \
  -d '{"query_text":"BC-8J3HBBBX","customer_email":"someone@example.com"}'

# the trigger function is no longer reachable either -> PGRST202
q -X POST "$SB/rpc/bc_sync_customer_ticket_count" -H 'Content-Type: application/json' -d '{}'

# but the tracking lookup IS public, on purpose, and returns the shipping address.
# If this ever stops returning an order, the storefront's tracking page is broken.
q -X POST "$SB/rpc/bc_track_order" -H 'Content-Type: application/json' \
  -d '{"p_order_number":"BC-8J3HBBBX"}'
```

*Last verified 2026-08-07.*
