#!/usr/bin/env python3
"""
Brasa Commerce — synthetic load against the production webhooks.

    python3 load.py --path checkout         # the staged ramp, no model in the chain
    python3 load.py --path intake           # the same ramp through the classifier
    python3 load.py --path checkout --cross # ...while watching Cedar on the same instance
    python3 load.py --n 12 --c 12           # one stage
    python3 load.py --dry-run               # print what it would send, send nothing

THIS HITS PRODUCTION AND HAS SIDE EFFECTS, and there is no staging environment: a
load test against a copy would not be a test of the thing being assured.

    checkout   1 order + items + shipment + run row, 1 email, NO model call
    intake     1 customer + ticket + run row, up to 2 emails, 1 DeepSeek completion

BUDGET BEFORE RUNNING. The binding limit is not the system, it is Resend's free
daily quota of 100 sends, shared with Cedar and Holt. Check what is left first —
a ramp that runs out mid-stage produces an error column that conflates "the system
broke" with "the plan ran out". Those are separable afterwards (a quota refusal
records `blocked`, not `error`, since assurance item 10) but not preventable.

Why two paths and not one
-------------------------
Cedar's load test could only say that its single webhook queued. Brasa has a path
with a model and a path without, on the same n8n instance, behind the same
Postgres. Running both answers a question one path cannot: is the ceiling the
orchestrator, or the model? If checkout — 500 ms of pure database work — saturates
at the same rate as intake, the model is not the constraint and no amount of
prompt tuning moves the number.

Why a ramp rather than one burst: the useful output is the name of whatever
degraded first, and that is easiest to read when the level it degraded at is
bracketed. Each stage stops the run if it errors, so nothing further is spent.

Overlap is measured, not assumed. Requests sent quickly in sequence are not load,
so each request's start and end are recorded and the maximum number in flight is
computed. If that number is 1, the stage proved nothing and says so.

Count the rows afterwards, do not trust the 200s. Both webhooks answer 200 for
"done" and for "already done" — Cedar's first load run reported 38 successes
against 20 rows, because its stages reused idempotency keys and the upper stages
were timing the dedup path. Every key here carries the stage id, and the script
prints the query that checks it.
"""
import argparse, concurrent.futures, json, sys, time
import urllib.request, urllib.error

BASE = "https://n8n-production-3503.up.railway.app/webhook"
OWNER = "mcruzcardoza@gmail.com"          # never a third party

MESSAGES = [
    "Does the 10 inch universal glass lid fit the No.10 cast iron skillet?",
    "How do I season a carbon steel wok for the first time?",
    "Is the enamelled dutch oven safe in the dishwasher?",
    "Which of your pans works on an induction hob?",
    "Do you ship to Canada, and what does delivery cost?",
    "What is your return policy if the pan is too heavy for me?",
]

FIRST = ["Marisol", "Tobias", "Neela", "Aurelio", "Fenna", "Cassian", "Ilse",
         "Rafael", "Odette", "Nikolai", "Simone", "Emeka", "Yara", "Lucian"]
LAST = ["Vasquez", "Brandt", "Okonjo", "Ferreira", "Halloran", "Nakamura",
        "Delgado", "Rousseau", "Sandoval", "Whitlock", "Amankwah", "Bergstrom"]

# (requests, concurrency). Ordered so the first stage that fails brackets the limit.
STAGES = [(6, 3), (10, 10)]


def person(i):
    name = f"{FIRST[i % len(FIRST)]} {LAST[(i * 5 + i // len(FIRST)) % len(LAST)]}"
    local, domain = OWNER.split("@")
    return name, f"{local}+{name.lower().replace(' ', '.')}@{domain}"


def body_for(path, key, i):
    name, email = person(i)
    if path == "checkout":
        return {"idempotency_key": key,
                "customer": {"full_name": name, "email": email, "phone": "+1 312 555 0147"},
                "shipping": {"line1": "1420 W Belmont Ave", "city": "Chicago",
                             "state": "IL", "postal_code": "60657", "country": "US"},
                "items": [{"sku": "BC-CI-10", "qty": 1}]}
    return {"eventId": "evt-" + key, "eventType": "FORM_RESPONSE", "data": {
        "responseId": key, "formId": "javEX9", "fields": [
            {"label": "Full name", "value": name},
            {"label": "Email address", "value": email},
            {"label": "Phone number", "value": "+1 312 555 0147"},
            {"label": "Message", "value": MESSAGES[i % len(MESSAGES)]}]}}


def fire(url, body, dry, timeout=180):
    if dry:
        t0 = time.monotonic(); time.sleep(0.05)
        return {"code": 200, "ms": 50, "t0": t0, "t1": time.monotonic(), "err": None, "dry": True}
    req = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    code, err = 0, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status; r.read()
    except urllib.error.HTTPError as e:
        code = e.code; err = e.read()[:200].decode("utf8", "replace")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return {"code": code, "ms": round((time.monotonic() - t0) * 1000),
            "t0": t0, "t1": time.monotonic(), "err": err}


def cedar_probe(tag, dry):
    """One Cedar intake, fired while Brasa is under load.

    All three builds share ONE n8n instance. A load test that only measures its own
    webhook cannot see that, and 'the neighbour gets slow' is the finding a portfolio
    of three builds on one runtime most needs.
    """
    sid = f"sub_bxload_{tag}"
    return fire(f"{BASE}/cedar-intake", {"eventId": "evt-" + sid, "data": {
        "submissionId": sid, "fields": [
            {"label": "Full name", "value": "Cross Load Probe"},
            {"label": "Email address", "value": f"mcruzcardoza+xload{tag}@gmail.com"},
            {"label": "Phone number", "value": "+1 512 555 0100"},
            {"label": "Tell us about your reason for visit",
             "value": "Stiff neck for four days, not painful, just tight when I turn my head."}]}}, dry)


def max_in_flight(rows):
    """What distinguishes load from a fast sequence."""
    ev = sorted([(r["t0"], 1) for r in rows] + [(r["t1"], -1) for r in rows])
    cur = peak = 0
    for _, d in ev:
        cur += d; peak = max(peak, cur)
    return peak


def pct(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def stage(path, n, c, tag, dry, cross):
    print(f"\n=== {path}: {n} requests, {c} at a time ===", flush=True)
    url = f"{BASE}/brasa-{path}"
    rows, base = [], 0
    xfut = None
    # The Cedar probe gets its OWN executor. Sharing one pool and adding a worker for it
    # let an extra Brasa request through: the first run of this script requested
    # concurrency 3 and measured a peak of 4 in flight. The stage would then have been
    # labelled with a concurrency it did not run at — the whole point of measuring the
    # peak rather than trusting the parameter.
    xpool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=c) as pool:
        futs = {pool.submit(fire, url, body_for(path, f"load-{tag}-c{c:02d}-{i:03d}", i), dry): i
                for i in range(n)}
        if cross:
            time.sleep(0.4)                      # let the burst actually be in flight
            xfut = xpool.submit(cedar_probe, f"{tag}c{c:02d}", dry)
        for f in concurrent.futures.as_completed(futs):
            r = f.result(); r["i"] = futs[f]; rows.append(r)
            flag = "" if r["code"] == 200 else "   <-- NOT 200"
            print(f"  {r['i']:03d}  HTTP {r['code']}  {r['ms']:6d} ms{flag}", flush=True)
            if r["err"]:
                print(f"        {r['err']}", flush=True)
    if xfut:
        base = xfut.result()
    xpool.shutdown()

    lat = [r["ms"] for r in rows]
    bad = [r for r in rows if r["code"] != 200]
    peak = max_in_flight(rows)
    wall = max(r["t1"] for r in rows) - min(r["t0"] for r in rows)

    print(f"\n  requested concurrency {c}, measured peak in flight {peak}")
    if peak < 2:
        print("  NOT LOAD — nothing overlapped. This stage proves nothing.")
    print(f"  wall clock {wall:.1f}s   throughput {len(rows)/wall:.2f} req/s")
    print(f"  latency ms  min {min(lat)}  p50 {pct(lat,.50)}  p95 {pct(lat,.95)}  max {max(lat)}")
    print(f"  errors {len(bad)}/{len(rows)} = {100*len(bad)/len(rows):.0f}%")
    if bad:
        codes = {}
        for r in bad:
            codes[r["code"]] = codes.get(r["code"], 0) + 1
        print(f"  status codes: {codes}")
    if base:
        print(f"  CEDAR, on the same n8n instance, during this burst: "
              f"HTTP {base['code']}  {base['ms']} ms")
    return rows, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", choices=["checkout", "intake"], default="checkout")
    ap.add_argument("--n", type=int)
    ap.add_argument("--c", type=int)
    ap.add_argument("--cross", action="store_true",
                    help="fire one Cedar intake inside each burst; costs 2 more emails per stage")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tag = time.strftime("%H%M%S")
    stages = [(a.n, a.c)] if a.n and a.c else STAGES
    per_email = 1 if a.path == "checkout" else 2
    total = sum(s[0] for s in stages)

    print(f"target: {BASE}/brasa-{a.path}")
    print(f"tag: {tag}   stages: {stages}   "
          f"{'DRY RUN' if a.dry_run else 'LIVE — writes rows, sends email'}")
    print(f"budget if every stage runs: {total} requests, up to {total*per_email} emails"
          + (f" + {2*len(stages)} for the Cedar probes" if a.cross else ""))

    allrows = []
    for sn, sc in stages:
        rows, bad = stage(a.path, sn, sc, tag, a.dry_run, a.cross)
        allrows += rows
        if bad:
            print("\n  stopping the ramp here: this stage produced errors, which is the answer.")
            break
        time.sleep(3)

    print(f"""
--- count the rows, do not trust the {len(allrows)} responses ---

  select stage, status, count(*) n,
         percentile_disc(0.5) within group (order by duration_ms) p50,
         percentile_disc(0.95) within group (order by duration_ms) p95
  from bc_workflow_runs where source_event_id like 'load-{tag}-%' group by 1,2 order by 1,2;

--- cleanup ---

  delete from bc_support_tickets where source_event_id like 'load-{tag}-%';
  delete from bc_orders           where source_event_id like 'load-{tag}-%';
  delete from bc_workflow_runs    where source_event_id like 'load-{tag}-%';
""")
    return 0 if all(r["code"] == 200 for r in allrows) else 1


if __name__ == "__main__":
    sys.exit(main())
