"""Staged synthetic load against the PRODUCTION Holt & Vargas intake webhook.

There is no staging environment. A load test against a copy would not be a test of
the thing being assured, so this runs against production and every row it writes is
identifiable by source_event_id 'load<tag>c<concurrency>i<index>'.

Two things this script is careful about, both learned the hard way on Cedar:

1. The stage and index are part of the idempotency key. If they were not, later
   stages would re-send earlier submissionIds, the workflow would correctly
   deduplicate them, and the upper stages would be timing the dedup path - which
   writes nothing, sends nothing and is very fast. The test would then report a
   flatteringly quick system under load. Count rows, never trust N x HTTP 200.

2. Overlap is measured, not assumed. Requests fired in a tight loop are not load
   if each finishes before the next begins. Each request's start and end are
   recorded and the true peak-in-flight is computed by sweep. A stage whose
   measured peak is 1 prints NOT LOAD and its latency figures are meaningless.

Usage:
    python3 load.py                 # 3 / 8 / 12
    python3 load.py 3 8             # custom stages
"""
import concurrent.futures
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

URL = "https://n8n-production-3503.up.railway.app/webhook/hvl-intake"
RECIPIENT = "mcruzcardoza+%s@gmail.com"   # owner's own address only, never a third party

CASES = [
    ("Asylum", "I have my I-589 application and my personal statement. I do not have police reports."),
    ("Family Visa", "I have the I-130 petition and our marriage certificate. Nothing else yet."),
    ("Naturalization", "I have my N-400 and five years of tax returns. I still need photos."),
    ("Employment-Based Green Card", "I have the I-140 approval notice and my passport copies."),
]


def payload(sid, i):
    case_type, desc = CASES[i % len(CASES)]
    return json.dumps({"eventId": sid, "data": {"submissionId": sid, "fields": [
        {"label": "Full name", "value": "Load Probe " + sid},
        {"label": "Email address", "value": RECIPIENT % sid},
        {"label": "Phone number", "value": "+1-212-555-0100"},
        {"label": "Case type", "value": ["o1"], "options": [{"id": "o1", "text": case_type}]},
        {"label": "Document description", "value": desc},
        {"label": "Country of origin", "value": "Colombia"},
    ]}}).encode()


def fire(args):
    sid, i = args
    req = urllib.request.Request(URL, data=payload(sid, i),
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            code = r.status
            r.read()
    except urllib.error.HTTPError as e:
        code = e.code
        e.read()
    except Exception as e:
        code = "EXC:" + type(e).__name__
    t1 = time.monotonic()
    return {"sid": sid, "code": code, "start": t0, "end": t1, "ms": int((t1 - t0) * 1000)}


def peak_in_flight(rows):
    """True maximum overlap, by sweep over start/end events."""
    events = []
    for r in rows:
        events.append((r["start"], 1))
        events.append((r["end"], -1))
    events.sort(key=lambda e: (e[0], -e[1]))
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def pct(values, p):
    if not values:
        return 0
    values = sorted(values)
    k = (len(values) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return int(values[lo] + (values[hi] - values[lo]) * (k - lo))


def stage(concurrency, tag):
    ids = [("load%sc%02di%02d" % (tag, concurrency, i), i) for i in range(concurrency)]
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        rows = list(ex.map(fire, ids))
    wall = time.monotonic() - t0

    peak = peak_in_flight(rows)
    ms = [r["ms"] for r in rows]
    errors = [r for r in rows if r["code"] != 200]

    print("\n--- concurrency %d ---" % concurrency)
    if peak < 2:
        print("  NOT LOAD - measured peak in flight was %d. This stage proves nothing." % peak)
    print("  requested %d   measured peak in flight %d" % (concurrency, peak))
    print("  client p50 %6d ms   p95 %6d ms   max %6d ms" % (pct(ms, 50), pct(ms, 95), max(ms)))
    print("  wall %5.1f s   throughput %.2f req/s   errors %d" % (
        wall, concurrency / wall if wall else 0, len(errors)))
    for e in errors:
        print("    ERROR %s %s" % (e["sid"], e["code"]))
    print("  ids: %s .. %s" % (rows[0]["sid"], rows[-1]["sid"]))
    return rows


def main():
    stages = [int(a) for a in sys.argv[1:]] or [3, 8, 12]
    tag = time.strftime("%H%M%S")
    print("tag        : %s" % tag)
    print("stages     : %s   total requests %d   emails ~%d"
          % (stages, sum(stages), sum(stages) * 2))
    print("target     : %s" % URL)

    allrows = []
    for c in stages:
        allrows += stage(c, tag)
        time.sleep(3)

    print("\n=== totals ===")
    print("  %d requests, %d non-200" % (len(allrows), len([r for r in allrows if r["code"] != 200])))
    print("  verify server-side with:")
    print("    select source_event_id, status, duration_ms from hvl_workflow_runs")
    print("     where source_event_id like 'load%s%%' order by source_event_id;" % tag)
    print("  the row count MUST equal %d - anything less is silent deduplication" % len(allrows))


main()
