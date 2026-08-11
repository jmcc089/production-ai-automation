#!/usr/bin/env python3
"""Put the n8n workflows under version control, and tell you when the live one has drifted.

Why this exists: on 2026-08-09 the live intake workflow silently reverted to an older
revision. Five nodes and the whole execution record disappeared, the model call narrowed
from 45 s to 25 s, and **nothing reported it** — every response body was still correct.
It ran degraded for hours and was only noticed by accident. Recovery depended entirely on a
backup somebody had happened to take by hand four hours earlier. There was no diff to read,
no history to bisect and no way to answer "what changed".

    python3 sync.py check      # live vs repo. Exits 1 on drift. Reads nothing secret.
    python3 sync.py pull       # write the repo copy from live, after you meant to change it
    python3 sync.py push       # deploy the repo copy to live

`check` is the one to run often, and the one to put in front of any debugging session:
"is the thing I am about to debug the thing this repository describes?"

Needs N8N_API_KEY and N8N_BASE in the environment. Nothing secret is written to the repo:
the workflow JSON stores `{{ $env.SUPABASE_SERVICE_KEY }}` as an expression, never a value,
and `verify_clean()` below refuses to write a file that contains a credential-shaped string
rather than trusting that to remain true.
"""
import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# Every workflow on the shared instance. Holt was put here first because Holt is the one
# that silently reverted; Cedar and Brasa sit on the same n8n and were exposed to exactly
# the same accident with nothing watching.
#
# Holt is one workflow, not two: the failure recorder used to be separate, pointed at by
# `settings.errorWorkflow`. On 2026-08-10 its three nodes were folded onto the intake canvas
# and the pointer aimed at the workflow itself, which n8n permits and Cedar already did. The
# split was what once let the recorder sit inactive while reporting no problem.
WORKFLOWS = {
    "holt-intake.json": "xurYOv7EzKPumZq0",
    "cedar-intake.json": "lj0aSfymkMyVOBRZ",
    "brasa-support.json": "1Ta9kHrR2dk8akKd",
    "brasa-checkout.json": "fDOqhDGup7859mo3",
}

# n8n refuses writes to an archived workflow — PUT returns 400 "Cannot update an archived
# workflow". `check` still reads them, which is the point: an archived workflow is exactly
# the one nobody looks at until the day it is switched back on.
ARCHIVED_OK = {"brasa-checkout.json"}

# Fields n8n changes on its own — timestamps, version counters, ownership. Keeping them
# would make every diff noise and hide the one line that matters.
VOLATILE = {"updatedAt", "createdAt", "versionId", "activeVersionId", "versionCounter",
            "triggerCount", "shared", "activeVersion", "isArchived", "pinData", "tags",
            "staticData", "meta", "description"}

# n8n's PUT accepts only these four keys, and `settings` only these two. Sending anything
# else back returns 400.
PUT_KEYS = ("name", "nodes", "connections", "settings")
SETTINGS_KEYS = ("executionOrder", "errorWorkflow")

SECRET_SHAPES = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"), "a JWT"),
    (re.compile(r"sb_secret_[A-Za-z0-9_-]{10,}"), "a Supabase secret key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "an API key"),
    (re.compile(r"\bre_[A-Za-z0-9]{20,}"), "a Resend key"),
]


def api(method, path, body=None):
    key, base = os.environ.get("N8N_API_KEY"), os.environ.get("N8N_BASE")
    if not key or not base:
        sys.exit("set N8N_API_KEY and N8N_BASE in the environment")
    req = urllib.request.Request(
        base.rstrip("/") + path, method=method,
        headers={"X-N8N-API-KEY": key, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def normalise(w):
    """The workflow as it is meant to be reviewed: everything that defines behaviour,
    nothing that changes by itself."""
    return {k: v for k, v in sorted(w.items()) if k not in VOLATILE}


def verify_clean(text, where):
    for pattern, what in SECRET_SHAPES:
        if pattern.search(text):
            sys.exit(f"REFUSING to write {where}: it contains {what}. "
                     "A secret belongs in an n8n credential or an env var, never in the repo.")


def dump(w):
    return json.dumps(normalise(w), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def cmd_pull():
    for fname, wid in WORKFLOWS.items():
        status, live = api("GET", f"/api/v1/workflows/{wid}")
        if status != 200:
            sys.exit(f"{fname}: GET returned {status}")
        text = dump(live)
        verify_clean(text, fname)
        open(os.path.join(HERE, fname), "w", encoding="utf-8").write(text)
        print(f"  {fname:<24} {len(live['nodes']):>3} nodes   written")
    print("\nReview the diff before committing. A pull records a change; it does not bless it.")


def cmd_check():
    drift = False
    for fname, wid in WORKFLOWS.items():
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            print(f"  {fname:<24} NOT IN REPO — run pull")
            drift = True
            continue
        status, live = api("GET", f"/api/v1/workflows/{wid}")
        if status != 200:
            print(f"  {fname:<24} GET returned {status}")
            drift = True
            continue
        want = open(path, encoding="utf-8").read()
        got = dump(live)
        if want == got:
            print(f"  {fname:<24} {len(live['nodes']):>3} nodes   matches repo")
            continue
        drift = True
        print(f"  {fname:<24} *** DRIFTED ***")
        w_names = [n["name"] for n in json.loads(want)["nodes"]]
        g_names = [n["name"] for n in live["nodes"]]
        lost, added = set(w_names) - set(g_names), set(g_names) - set(w_names)
        if lost:
            print(f"      nodes in repo but NOT live : {sorted(lost)}")
        if added:
            print(f"      nodes live but NOT in repo : {sorted(added)}")
        diff = list(difflib.unified_diff(want.splitlines(), got.splitlines(),
                                         "repo", "live", lineterm="", n=1))
        body = [l for l in diff[2:] if l.startswith(("+", "-"))]
        print(f"      {len(body)} changed lines; first 30:")
        for line in body[:30]:
            print("        " + line[:150])
    if drift:
        print("\nThe running system is not the one this repository describes.")
        print("Do NOT patch the difference node by node — a revert is a coherent change and")
        print("undoing half of it is worse than undoing none. Decide which side is right,")
        print("then `pull` (live was right) or `push` (repo was right).")
    return 1 if drift else 0


def cmd_push():
    for fname, wid in WORKFLOWS.items():
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            print(f"  {fname:<24} not in repo, skipped")
            continue
        want = json.loads(open(path, encoding="utf-8").read())
        status, live = api("GET", f"/api/v1/workflows/{wid}")
        if status != 200:
            sys.exit(f"{fname}: GET returned {status}")
        # errorWorkflow must survive: an unset pointer means failures are recorded nowhere,
        # and that is exactly how the Failure Recorder was found to be doing nothing.
        settings = {k: v for k, v in (want.get("settings") or live.get("settings") or {}).items()
                    if k in SETTINGS_KEYS}
        body = {k: want.get(k, live.get(k)) for k in PUT_KEYS}
        body["settings"] = settings
        status, resp = api("PUT", f"/api/v1/workflows/{wid}", body)
        if status != 200:
            if fname in ARCHIVED_OK and "archived" in str(resp).lower():
                print(f"  {fname:<24} ARCHIVED — unarchive it in n8n first, then push again")
                continue
            sys.exit(f"{fname}: PUT returned {status} {str(resp)[:300]}")
        _, after = api("GET", f"/api/v1/workflows/{wid}")
        ok = dump(after) == dump(want)
        print(f"  {fname:<24} pushed, {len(after['nodes'])} nodes, "
              f"errorWorkflow={after.get('settings', {}).get('errorWorkflow')}, "
              f"round-trips clean: {ok}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "pull":
        cmd_pull()
    elif cmd == "push":
        cmd_push()
    elif cmd == "check":
        sys.exit(cmd_check())
    else:
        sys.exit(__doc__)
