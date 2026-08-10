#!/usr/bin/env python3
"""
Holt & Vargas — completeness classifier evaluation.

Runs the *deployed* classifier against a frozen set of cases with expected outputs and
prints how many passed. It reads nothing from the database, writes nothing to it, and
sends no email: the only external call is to the model.

What is under test is the pair of n8n Code nodes that decide what a paralegal and a client
are told - `Build DeepSeek Body` and `Parse Deepseek Response`. Their source is copied into
deployed/ and hashed in deployed/SHA256SUMS, and nodes_shim.js executes it verbatim. A
reimplementation in Python would keep passing after the workflow changed, which is the one
failure mode an eval must not have.

  python3 run.py                     # the frozen set
  python3 run.py --holdout           # the holdout set, written after the rules were frozen
  python3 run.py --repeat 3          # same set N times, to show the instability band
  python3 run.py --case asylum-bare  # one case
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = "deepseek-v4-flash"   # --model overrides, for the item 11 comparison
ENDPOINT = "https://api.deepseek.com/chat/completions"
MAX_TOKENS = 5000          # the same ceiling the workflow sends
TIMEOUT = 45               # the same timeout the workflow sends


# ---------------------------------------------------------------- plumbing

def load_key():
    for path in (os.path.join(HERE, ".env"),
                 os.path.join(HERE, "..", "..", "brasa_commerce", "eval", ".env")):
        if os.path.exists(path):
            for line in open(path):
                if line.strip().startswith("DEEPSEEK_API_KEY"):
                    return line.split("=", 1)[1].strip()
    sys.exit("no DEEPSEEK_API_KEY found (expected in eval/.env)")


def verify_deployed():
    """The eval is worthless if deployed/ has drifted from what it claims to be."""
    sums = os.path.join(HERE, "deployed", "SHA256SUMS")
    ok = True
    for line in open(sums):
        want, name = line.split()
        got = hashlib.sha256(open(os.path.join(HERE, "deployed", name), "rb").read()).hexdigest()
        if got != want:
            print(f"  !! {name} does not match SHA256SUMS")
            ok = False
    return ok


def shim(mode, payload):
    p = subprocess.run(["node", os.path.join(HERE, "nodes_shim.js"), mode],
                       input=json.dumps(payload).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        sys.exit("nodes_shim.js failed: " + p.stderr.decode()[:500])
    return json.loads(p.stdout.decode())


def call_model(key, prompt):
    body = json.dumps({"model": MODEL,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": MAX_TOKENS}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read().decode())
        ch = d["choices"][0]
        u = d.get("usage", {})
        u["finish_reason"] = ch.get("finish_reason")
        return ch["message"]["content"], u, int((time.time() - t) * 1000), None
    except urllib.error.HTTPError as e:
        return "", {}, int((time.time() - t) * 1000), f"HTTP {e.code}"
    except Exception as e:                                   # timeout, connection reset
        return "", {}, int((time.time() - t) * 1000), type(e).__name__


# ---------------------------------------------------------------- criteria

def check(expect, p, checklist):
    """Returns (failures, notes). Each key is one stated criterion."""
    f = []
    present = p.get("catalogue_present") or []
    missing = p.get("catalogue_missing") or []
    off = p.get("offcatalogue") or []
    score = p.get("completeness_score")
    label = p.get("completeness_label")

    if "label" in expect and label not in expect["label"]:
        f.append(f"label {label!r} not in {expect['label']}")
    if "label_not" in expect and label in expect["label_not"]:
        f.append(f"label {label!r} is forbidden here")
    if "score_band" in expect:
        lo, hi = expect["score_band"]
        # A null score is the system declining to answer, which since 2026-08-09 is a real
        # outcome: an unparseable response and a case type with no checklist both produce
        # one. It satisfies a band only when the label admits it too — a refusal that still
        # carries a confident label is the failure this whole item is about, so the pair is
        # checked together rather than the number alone.
        if score is None:
            if label != "Unknown":
                f.append(f"score is null but label is {label!r}, not 'Unknown'")
        elif not (lo <= score <= hi):
            f.append(f"score {score} outside [{lo},{hi}]")
    for d in expect.get("present_includes", []):
        if d not in present:
            f.append(f"{d!r} should be present")
    for d in expect.get("missing_includes", []):
        if d not in missing:
            f.append(f"{d!r} should be missing")
    if "present_max" in expect and len(present) > expect["present_max"]:
        f.append(f"{len(present)} present, at most {expect['present_max']} expected")
    if "missing_max" in expect and len(missing) > expect["missing_max"]:
        f.append(f"{len(missing)} missing, at most {expect['missing_max']} expected")
    if "missing_min" in expect and len(missing) < expect["missing_min"]:
        f.append(f"{len(missing)} missing, at least {expect['missing_min']} expected")
    if "language" in expect and p.get("preferred_language") != expect["language"]:
        f.append(f"language {p.get('preferred_language')!r} != {expect['language']!r}")
    if "contract_violated" in expect and bool(p.get("contract_violated")) != expect["contract_violated"]:
        f.append(f"contract_violated {p.get('contract_violated')} != {expect['contract_violated']}")
    if "offcatalogue_max" in expect and len(off) > expect["offcatalogue_max"]:
        f.append(f"{len(off)} off-catalogue items, at most {expect['offcatalogue_max']} expected")
    if "catalogue_size" in expect and len(checklist) != expect["catalogue_size"]:
        f.append(f"catalogue has {len(checklist)}, expected {expect['catalogue_size']}")
    if expect.get("client_sees_only_catalogue"):
        leaked = [d for d in present + missing if d not in checklist]
        if leaked:
            f.append(f"model text reached the client: {leaked}")
    # contract_violated_either is satisfied by construction; it records that both
    # outcomes were judged acceptable in advance rather than after seeing the result.
    return f


# ---------------------------------------------------------------- run

def run_once(cases, key, workers=3):
    prompts = shim("prompt", cases)
    by_id = {p["id"]: p for p in prompts}

    def work(pr):
        # One retry: a provider timeout is the model's queue, not the classifier's verdict,
        # and a set that fails on it stops measuring the thing it is for.
        content, usage, ms, err = call_model(key, pr["deepseek_body"])
        if err:
            time.sleep(2)
            content, usage, ms2, err = call_model(key, pr["deepseek_body"])
            ms += ms2
        return pr["id"], content, usage, ms, err

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for cid, content, usage, ms, err in ex.map(work, prompts):
            results[cid] = (content, usage, ms, err)

    parse_in = [{"id": c["id"], "content": results[c["id"]][0],
                 "fields": by_id[c["id"]]["fields"], "checklist": by_id[c["id"]]["checklist"]}
                for c in cases]
    parsed = {r["id"]: r["parsed"] for r in shim("parse", parse_in)}

    out = []
    for c in cases:
        content, usage, ms, err = results[c["id"]]
        fails = check(c["expect"], parsed[c["id"]], by_id[c["id"]]["checklist"])
        out.append({"id": c["id"], "fails": fails, "ms": ms, "err": err,
                    "tokens": usage.get("total_tokens"),
                    "finish": usage.get("finish_reason"),
                    "completion": usage.get("completion_tokens"),
                    "parsed": parsed[c["id"]]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--case")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--model", help="override the model, e.g. deepseek-v4-pro")
    a = ap.parse_args()

    global MODEL
    if a.model:
        MODEL = a.model
    fname = "cases_holdout.json" if a.holdout else "cases.json"
    path = os.path.join(HERE, fname)
    raw = open(path, "rb").read()
    cases = json.loads(raw)
    if a.case:
        cases = [c for c in cases if c["id"] == a.case] or sys.exit("no such case")

    print(f"set        : {fname}  ({len(cases)} cases)")
    print(f"sha256     : {hashlib.sha256(raw).hexdigest()}")
    print(f"model      : {MODEL}  max_tokens={MAX_TOKENS}")
    print(f"deployed   : {'matches SHA256SUMS' if verify_deployed() else 'DRIFTED — results are not about the live system'}")
    print()

    key = load_key()
    passed_runs = []
    for rep in range(a.repeat):
        res = run_once(cases, key)
        npass = sum(1 for r in res if not r["fails"] and not r["err"])
        passed_runs.append(npass)
        print(f"--- run {rep + 1} of {a.repeat} ---")
        for r in res:
            if r["err"]:
                mark, detail = "ERR ", f"model call failed: {r['err']}"
            elif r["fails"]:
                mark = "FAIL"
                detail = f"[finish={r.get('finish')} completion={r.get('completion')}] " + "; ".join(r["fails"])
            else:
                p = r["parsed"]
                mark = "pass"
                sc = p.get("completeness_score")
                shown = "no score" if sc is None else f"{sc}/100"
                basis = f"  ({p['score_basis']})" if p.get("score_basis") else ""
                detail = f"{shown} {p.get('completeness_label')}{basis}"
            print(f"  {mark}  {r['id']:<26} {detail}")
            if a.verbose and not r["err"]:
                p = r["parsed"]
                print(f"        present={p.get('catalogue_present')}")
                print(f"        off    ={p.get('offcatalogue')}")
        lat = sorted(r["ms"] for r in res)
        tok = [r["tokens"] for r in res if r["tokens"]]
        print(f"  {npass}/{len(cases)} passed   "
              f"median {lat[len(lat) // 2]} ms   "
              f"max {lat[-1]} ms   "
              f"mean {sum(tok) // len(tok) if tok else 0} tokens")
        print()

    if a.repeat > 1:
        print(f"instability band over {a.repeat} runs: "
              f"{min(passed_runs)}-{max(passed_runs)} of {len(cases)}")
    return 0 if passed_runs[-1] == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
