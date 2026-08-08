#!/usr/bin/env python3
"""
Brasa Commerce — what a request costs in time, measured against production.

    python3 latency.py                  # all three paths, sequential
    python3 latency.py --n 20           # how many intakes (default 20)
    python3 latency.py --only intake    # intake | checkout | skipped | rejected
    python3 latency.py --dry-run        # print the payloads, send nothing

THIS HITS PRODUCTION AND HAS SIDE EFFECTS. Each intake spends one DeepSeek
completion and attempts up to two Resend emails; each checkout writes a real
order. Every row it creates is identifiable — `source_event_id` begins with
`lat-` — and the clean-up SQL is printed at the end.

Every request comes from a DIFFERENT generated customer, and that is not
cosmetic. The customer upsert is keyed on the email address, so a batch sent
from one address folds onto one `bc_customers` row: the first version of this
script left a single customer holding 123 tickets and 29 orders, which is
correct behaviour and an unreadable record.

By default the deliverable address is a plus-alias of the owner's own mailbox —
`mcruzcardoza+marisol.vasquez@gmail.com` — so each run gets its own customer row
and NOTHING can reach a third party. `--fake-recipients` swaps in
`first.last@email.com`, which is the convention the seed data uses and reads
better in the table, at the cost of Resend attempting delivery to a domain
nobody here controls. That is a deliberate choice, not a default.

Why it measures four paths and not one
--------------------------------------
The interesting number is not the average request. It is the spread between the
paths, because three of Brasa's four are not the one people picture:

  intake    webhook -> guard -> two retrievals -> DeepSeek -> writes -> 2 emails
  skipped   the same, minus the model: a message with fewer than ten letters or
            digits is routed past DeepSeek to Canned Classification (item 09).
            The difference between this and `intake` IS the model's latency, and
            it is measured rather than reasoned about.
  checkout  no model at all. Prices resolved server-side against the catalog.
  rejected  refused by Guard Intake before anything is written or sent.

What it reports, and what it does not
-------------------------------------
Client wall clock, which is what a caller experiences, including the network.
The server's own view is in `bc_workflow_runs.duration_ms` and the script prints
the query to compare them. Two independent measurements that agree are the point;
one that agrees with itself is not evidence.

It reports percentiles over sequential requests. Sequential is deliberate:
concurrency is item 12's question, and mixing the two produces a number that
answers neither.

Requests are sent one at a time with no retry. A non-2xx is recorded and the run
continues — a probe that stops at the first failure cannot measure a tail.
"""
import argparse, json, statistics, sys, time, urllib.error, urllib.request

BASE = "https://n8n-production-3503.up.railway.app/webhook"
OWNER = "mcruzcardoza@gmail.com"          # never a third party — see the docstring

# Real questions of varying length, because prompt tokens scale with the message
# and latency scales with the completion. A single repeated message would measure
# DeepSeek's cache rather than this system.
MESSAGES = [
    "Does the 10 inch universal glass lid fit the No.10 cast iron skillet?",
    "What is the difference between the carbon steel and the cast iron pan for a gas hob?",
    "How do I season a carbon steel wok for the first time, and how often after that?",
    "Is the enamelled dutch oven safe to put in the dishwasher or does that ruin the finish?",
    "Do you ship to Canada, and roughly what does delivery cost and take?",
    "I want to buy a gift for someone who has just moved into their first flat. What would you suggest under a hundred dollars?",
    "Can the cast iron skillet go straight from the hob into the oven at 250 C?",
    "What size wok would you recommend for a household of two who mostly cook stir fries?",
    "Is there a warranty on the enamel, and what does it actually cover?",
    "How long does it normally take for an order to leave your warehouse?",
    "The handle on the skillet gets extremely hot. Is that expected or is something wrong with mine?",
    "Do you sell replacement lids on their own, or only as part of a set?",
    "What is your return policy if I open the box and decide the pan is too heavy for me?",
    "Can I use metal utensils on the carbon steel, or will that strip the seasoning?",
    "Which of your pans works on an induction hob?",
    "Is the cast iron pre-seasoned when it arrives or do I need to do that myself before the first use?",
    "I am comparing your 12 inch skillet against a competitor's. What makes yours worth the difference in price?",
    "How should I store the wok so it does not rust in a humid kitchen?",
    "Do you offer gift wrapping or a note in the box?",
    "What is the weight of the 10 inch skillet? I have some wrist trouble and want to be sure I can lift it.",
]

# Fewer than ten letters or digits, so Has Substance? routes past the model.
JUNK = ["...", "?", "hi"]

# Refused by Guard Intake before anything is written, retrieved or sent.
BAD = [{"eventId": "lat-bad-1", "hello": "world"},
       {"eventId": "lat-bad-2", "eventType": "FORM_RESPONSE",
        "data": {"responseId": "lat-bad-2", "fields": [
            {"label": "Email address", "value": ""},
            {"label": "Message", "value": "no address on this one"}]}}]


FIRST = ["Marisol", "Tobias", "Neela", "Aurelio", "Fenna", "Cassian", "Ilse",
         "Rafael", "Odette", "Nikolai", "Simone", "Emeka", "Yara", "Lucian"]
LAST = ["Vasquez", "Brandt", "Okonjo", "Ferreira", "Halloran", "Nakamura",
        "Delgado", "Rousseau", "Sandoval", "Whitlock", "Amankwah", "Bergstrom"]


def person(i, fake):
    """A distinct customer per request. See the docstring on why this matters."""
    name = f"{FIRST[i % len(FIRST)]} {LAST[(i * 5 + i // len(FIRST)) % len(LAST)]}"
    handle = name.lower().replace(" ", ".")
    local, domain = OWNER.split("@")
    return name, (f"{handle}@email.com" if fake else f"{local}+{handle}@{domain}")


def intake_body(tag, message, who):
    name, email = who
    return {"eventId": tag, "eventType": "FORM_RESPONSE", "data": {
        "responseId": tag, "formId": "javEX9", "fields": [
            {"key": "q1", "label": "Full name", "type": "INPUT_TEXT", "value": name},
            {"key": "q2", "label": "Email address", "type": "INPUT_EMAIL", "value": email},
            {"key": "q3", "label": "Phone number", "type": "INPUT_PHONE_NUMBER", "value": "+1 312 555 0147"},
            {"key": "q4", "label": "Message", "type": "TEXTAREA", "value": message}]}}


def checkout_body(tag, who):
    name, email = who
    return {"idempotency_key": tag,
            "customer": {"full_name": name, "email": email, "phone": "+1 312 555 0147"},
            "shipping": {"line1": "1420 W Belmont Ave", "city": "Chicago",
                         "state": "IL", "postal_code": "60657", "country": "US"},
            "items": [{"sku": "BC-CI-10", "qty": 1}]}


def fire(path, body, timeout=90):
    """One request, one wall-clock reading. A failure is a data point, not a stop."""
    req = urllib.request.Request(f"{BASE}/{path}", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ms": int((time.time() - t0) * 1000), "code": r.status,
                    "body": r.read(400).decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"ms": int((time.time() - t0) * 1000), "code": e.code,
                "body": e.read(400).decode("utf-8", "replace")}
    except Exception as e:
        return {"ms": int((time.time() - t0) * 1000), "code": 0, "body": repr(e)}


def pct(xs, p):
    return sorted(xs)[min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))]


def report(label, rows):
    if not rows:
        return
    ms = [r["ms"] for r in rows]
    codes = {}
    for r in rows:
        codes[r["code"]] = codes.get(r["code"], 0) + 1
    print(f"{label:<10} n={len(ms):<3} min {min(ms):>6}  p50 {pct(ms,50):>6}  "
          f"p90 {pct(ms,90):>6}  p95 {pct(ms,95):>6}  max {max(ms):>6}   "
          f"codes {dict(sorted(codes.items()))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--only", choices=["intake", "skipped", "checkout", "rejected"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fake-recipients", action="store_true",
                    help="use first.last@email.com instead of owner plus-aliases; "
                         "reads better in the table, mails a domain nobody here owns")
    a = ap.parse_args()

    run = int(time.time())
    seq = iter(range(10_000))
    plan = []
    if a.only in (None, "intake"):
        for i in range(a.n):
            plan.append(("intake", "brasa-intake",
                         intake_body(f"lat-{run}-i{i}", MESSAGES[i % len(MESSAGES)],
                                     person(next(seq), a.fake_recipients))))
    if a.only in (None, "skipped"):
        for i, m in enumerate(JUNK):
            plan.append(("skipped", "brasa-intake",
                         intake_body(f"lat-{run}-s{i}", m, person(next(seq), a.fake_recipients))))
    if a.only in (None, "checkout"):
        for i in range(max(1, a.n // 2)):
            plan.append(("checkout", "brasa-checkout",
                         checkout_body(f"lat-{run}-c{i}", person(next(seq), a.fake_recipients))))
    if a.only in (None, "rejected"):
        for i, b in enumerate(BAD):
            b = dict(b); b["eventId"] = f"lat-{run}-r{i}"
            plan.append(("rejected", "brasa-intake", b))

    if a.dry_run:
        for kind, path, body in plan:
            print(kind, path, json.dumps(body)[:160])
        return 0

    print(f"{len(plan)} sequential requests against production · run tag lat-{run}\n")
    results = {}
    for n, (kind, path, body) in enumerate(plan, 1):
        r = fire(path, body)
        results.setdefault(kind, []).append(r)
        print(f"  {n:>3}/{len(plan)}  {kind:<9} {r['code']}  {r['ms']:>6} ms")

    print()
    for kind in ("intake", "skipped", "checkout", "rejected"):
        report(kind, results.get(kind, []))

    if results.get("intake") and results.get("skipped"):
        d = statistics.median([r["ms"] for r in results["intake"]]) - \
            statistics.median([r["ms"] for r in results["skipped"]])
        print(f"\nthe model's share of an intake, at p50: {d:.0f} ms")

    print(f"""
Compare against the server's own measurement — two clocks, one interval:

  select stage, status, count(*), round(avg(duration_ms)) avg_ms,
         percentile_disc(0.5) within group (order by duration_ms) p50,
         percentile_disc(0.95) within group (order by duration_ms) p95,
         round(avg(total_tokens)) avg_tokens
  from bc_workflow_runs where source_event_id like 'lat-{run}-%' group by 1,2;

Clean-up, when the evidence is no longer needed:

  delete from bc_support_tickets where source_event_id like 'lat-{run}-%';
  delete from bc_orders           where source_event_id like 'lat-{run}-%';
  delete from bc_workflow_runs    where source_event_id like 'lat-{run}-%';
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
