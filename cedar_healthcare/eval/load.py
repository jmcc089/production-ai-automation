#!/usr/bin/env python3
"""
Cedar Healthcare — synthetic load against the production intake webhook.

    python3 load.py                     # the staged ramp described below
    python3 load.py --n 20 --c 20       # one stage: 20 requests, 20 at a time
    python3 load.py --dry-run           # print what it would send, send nothing

THIS HITS PRODUCTION AND HAS SIDE EFFECTS. Every request that succeeds writes a
ch_patients row, a ch_intake_requests row, a ch_workflow_runs row, and sends TWO emails
through Resend - one to the patient address, one to the practitioner. There is no staging
environment; a load test against a local instance would not be a test of the deployed
system, which is the thing being assured.

Budget before running: requests x 2 emails, against Resend's 10 requests/second per team.
That team is shared with the other two builds on this n8n instance.

Clean up afterwards with the SQL printed at the end.

Why a ramp rather than one burst: the useful output of a load test is the name of whatever
degraded first, and that is easiest to read when the level it degraded at is bracketed. Each
stage stops the run if it produces errors, so the first stage that breaks is the answer and
nothing further is spent.

Overlap is measured, not assumed. Requests sent quickly in sequence are not load, so the
script records each request's start and end and computes the maximum number in flight at
once. If that number is 1, the stage proved nothing and says so.
"""

import json, sys, time, urllib.request, urllib.error, concurrent.futures, statistics

URL = "https://n8n-production-3503.up.railway.app/webhook/cedar-intake"

MESSAGES = [
    "I twisted my ankle badly yesterday and cannot put weight on it. Can I be seen today?",
    "My lower back has ached for six weeks and it is slowly getting worse at my desk.",
    "I would like to book a routine sports massage. I cycle at weekends.",
    "My GP suggested I speak to a nutritionist about my diet and blood sugar.",
    "Stiff neck for four days, not painful, just tight when I turn my head.",
    "Seven weeks after knee surgery my surgeon cleared me for rehab. Stairs are hard.",
    "I train four times a week and my shoulders are constantly tight. Nothing injured.",
    "Bloated after every meal for a month and I have lost five kilos without trying.",
    "Could you tell me what services you offer and roughly what they cost?",
    "My shoulder has hurt since I painted a ceiling. It wakes me at night now.",
]

# (requests, concurrency). Ordered so the first stage that fails brackets the limit.
STAGES = [(6, 3), (12, 12), (20, 20)]


def fire(i, tag, dry, stage_id=""):
    # The stage id is part of the key. Without it every stage reused indices 0..n-1 under the
    # same tag, so the later stages re-sent earlier submissionIds and were deduplicated: on
    # 2026-08-07 a 38-request ramp produced 20 rows, and the upper stages were timing the
    # dedup path - which does no insert and sends no email - mixed in with real work. Caught
    # by counting rows in the database rather than by trusting 38 HTTP 200s.
    sid = f"sub_load_{tag}_{stage_id}_{i:03d}"
    body = {"eventId": "evt-" + sid, "data": {"submissionId": sid, "fields": [
        {"label": "Full name", "value": f"Load Probe {tag} {stage_id} {i:03d}"},
        {"label": "Email address", "value": f"mcruzcardoza+load{tag}{stage_id}{i:03d}@gmail.com"},
        {"label": "Phone number", "value": "+1 512 555 0100"},
        {"label": "Tell us about your reason for visit", "value": MESSAGES[i % len(MESSAGES)]}]}}
    if dry:
        t0 = time.monotonic()
        time.sleep(0.2)
        return {"i": i, "code": 200, "ms": 200, "t0": t0, "t1": time.monotonic(),
                "err": None, "dry": True}
    data = json.dumps(body).encode()
    req = urllib.request.Request(URL, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    code, err = None, None
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            code = r.status
            r.read()
    except urllib.error.HTTPError as e:
        code = e.code
        err = e.read()[:200].decode("utf8", "replace")
    except Exception as e:
        code = 0
        err = f"{type(e).__name__}: {e}"
    t1 = time.monotonic()
    return {"i": i, "code": code, "ms": round((t1 - t0) * 1000), "t0": t0, "t1": t1, "err": err}


def max_in_flight(rows):
    """Maximum number of requests open at the same instant, from the recorded intervals.
    This is what distinguishes load from a fast sequence."""
    events = []
    for r in rows:
        events.append((r["t0"], 1))
        events.append((r["t1"], -1))
    events.sort()
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return peak


def pct(xs, p):
    s = sorted(xs)
    if not s:
        return None
    k = max(1, min(len(s), -(-int(p * len(s)) // 1)))
    return s[int(k) - 1]


def stage(n, c, tag, dry):
    print(f"\n=== stage: {n} requests, {c} at a time ===", flush=True)
    rows = []
    sid = f"c{c:02d}"
    with concurrent.futures.ThreadPoolExecutor(max_workers=c) as pool:
        futs = [pool.submit(fire, i, tag, dry, sid) for i in range(n)]
        for f in concurrent.futures.as_completed(futs):
            r = f.result()
            rows.append(r)
            flag = "" if r["code"] == 200 else "   <-- NOT 200"
            if r.get("dry"):
                flag = "   (dry)"
            print(f"  {r['i']:03d}  HTTP {r['code']}  {r['ms']:6d} ms{flag}", flush=True)
            if r["err"]:
                print(f"        {r['err']}", flush=True)

    lat = [r["ms"] for r in rows]
    bad = [r for r in rows if r["code"] != 200]
    peak = max_in_flight(rows)
    wall = max(r["t1"] for r in rows) - min(r["t0"] for r in rows)

    print(f"\n  requested concurrency {c}, measured peak in flight {peak}")
    if peak < 2:
        print("  NOT LOAD — nothing overlapped. This stage proves nothing.")
    print(f"  wall clock {wall:.1f}s   throughput {len(rows)/wall:.2f} req/s")
    print(f"  latency ms  min {min(lat)}  p50 {pct(lat,0.50)}  p95 {pct(lat,0.95)}  max {max(lat)}")
    print(f"  errors {len(bad)}/{len(rows)} = {100*len(bad)/len(rows):.0f}%")
    if bad:
        codes = {}
        for r in bad:
            codes[r["code"]] = codes.get(r["code"], 0) + 1
        print(f"  status codes: {codes}")
    return rows, bad, peak


def main():
    dry = "--dry-run" in sys.argv
    tag = time.strftime("%H%M%S")
    n = c = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--n":
            n = int(sys.argv[i + 2])
        if a == "--c":
            c = int(sys.argv[i + 2])
    stages = [(n, c)] if n and c else STAGES

    print(f"target: {URL}")
    print(f"tag: {tag}   stages: {stages}   {'DRY RUN' if dry else 'LIVE - writes rows, sends email'}")
    total = sum(s[0] for s in stages)
    print(f"budget if every stage runs: {total} requests, about {total*2} emails")

    allrows = []
    for sn, sc in stages:
        rows, bad, peak = stage(sn, sc, tag, dry)
        allrows += rows
        if bad:
            print("\n  stopping the ramp here: this stage produced errors, which is the answer.")
            break
        time.sleep(3)

    print(f"\n--- cleanup ---")
    print("Rows written by this run all carry a source_event_id starting 'sub_load_'.")
    print("""
delete from ch_workflow_runs where source_event_id like 'sub_load_%';
delete from ch_intake_requests where source_event_id like 'sub_load_%';
delete from ch_patients where email like 'mcruzcardoza+load%';
""")
    print("Then read what the system recorded about itself under load:")
    print("""
select status, stage, error_node, count(*), round(avg(duration_ms)) as avg_ms
from ch_workflow_runs where source_event_id like 'sub_load_%'
group by 1,2,3 order by 4 desc;
""")
    return 0 if all(r["code"] == 200 for r in allrows) else 1


if __name__ == "__main__":
    sys.exit(main())
