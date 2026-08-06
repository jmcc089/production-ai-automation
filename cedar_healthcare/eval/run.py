#!/usr/bin/env python3
"""
Cedar Healthcare — intake classifier evaluation.

Runs every case in cases.json against the live Deepseek classifier, applies the same
validation the n8n workflow applies, and reports how many cases passed.

No database writes, no emails, no rows created. It exercises the classification decision,
which is the part that can be *wrong* rather than merely *fail*.

    python3 run.py                  # all cases, one run each
    python3 run.py physio           # only cases whose id contains "physio"
    python3 run.py --repeat 5       # five runs per case, reporting which answers vary
    python3 run.py --set holdout    # run cases_holdout.json instead of cases.json
    python3 run.py --check-catalog  # compare catalog.json against the live ch_services table

A single green run says the answers were acceptable once. It says nothing about whether the
same message gets the same answer twice, and at least one case in this set does not — use
--repeat before trusting a pass.

Requires cedar_healthcare/eval/.env (gitignored). One secret:

    DEEPSEEK_API_KEY=...

Only --check-catalog reads a database credential, and only if you add SUPABASE_URL and
SUPABASE_SERVICE_KEY yourself. A normal run needs neither.

Exit code is 0 only if every case passed.

KNOWN LIMIT — read this before trusting a green run.
The prompt below is a copy of the one built by the n8n `Build Deepseek Body` node, and the
validation is a copy of `Parse Deepseek Response`. They can drift. The variable half cannot:
both read the service catalog from ch_services at run time, so adding or editing a service is
reflected here automatically. The fixed half — the urgency, complexity and output-shape text —
is duplicated, and a change to the node that is not mirrored here will make this suite pass
while production behaves differently.
"""

import json, os, re, sys, urllib.request, urllib.error, pathlib, concurrent.futures

HERE = pathlib.Path(__file__).parent
GREETINGS = ("hi ", "hi,", "hello", "dear ", "good morning", "good afternoon")


def load_env():
    """Reads .env into os.environ. The value of DEEPSEEK_API_KEY is used in exactly one
    place — the Authorization header in classify() — and is never printed, logged or
    written anywhere by this script."""
    f = HERE / ".env"
    if not f.exists():
        sys.exit("Missing cedar_healthcare/eval/.env — see the docstring in this file.")
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("DEEPSEEK_API_KEY is not set in cedar_healthcare/eval/.env")


def post(url, payload, headers, timeout=90):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_catalog():
    """The service catalog the prompt is built from.

    Read from catalog.json, a committed snapshot of ch_services. It is not secret — four
    service names and their prompt descriptions — and keeping it local means this suite
    needs no database credential at all.

    The cost is that the snapshot can fall behind the table. `--check-catalog` compares
    them, and needs SUPABASE_URL and SUPABASE_SERVICE_KEY set; without that flag no
    Supabase credential is read or required.
    """
    rows = json.loads((HERE / "catalog.json").read_text())
    if not rows:
        sys.exit("catalog.json is empty — the classifier cannot be evaluated.")
    return rows


def check_catalog(local):
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    base = os.environ.get("SUPABASE_URL")
    if not (key and base):
        sys.exit("--check-catalog needs SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    url = (base.rstrip("/") +
           "/rest/v1/ch_services?select=name,assignable,prompt_hint&order=assignable.desc,name")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        live = json.loads(r.read())
    norm = lambda rows: sorted((x["name"], x["assignable"], x["prompt_hint"]) for x in rows)
    if norm(live) == norm(local):
        print(f"catalog.json matches ch_services ({len(live)} services)")
        return 0
    print("catalog.json has DRIFTED from ch_services")
    print(f"  live:  {sorted(x['name'] for x in live)}")
    print(f"  local: {sorted(x['name'] for x in local)}")
    return 1


def build_prompt(catalog, message):
    """Mirror of the n8n Build Deepseek Body node."""
    block = "\n".join(f'- "{s["name"]}" - {s["prompt_hint"]}' for s in catalog)
    return f"""You are a medical intake classifier for Cedar Healthcare, a wellness clinic in Austin, Texas.

Your job is to read a patient's free-text intake message and return a structured JSON object. You must return JSON only - no preamble, no explanation, no markdown fences.

SERVICE CATEGORY
{block}

Classify on the condition the patient describes, not on the service they ask for. If they request a treatment this clinic does not offer but describe a condition that maps to a listed service, use that listed service. Use the non-service category only when the condition itself maps to none of them, or when it maps to more than one and the message gives no way to choose.

URGENCY LEVEL
- 3 (Acute) - patient describes active pain, inability to work or perform normal daily function, injury that just happened, or explicitly requests same-day/emergency appointment

A problem the patient has had for more than about two weeks is not level 3 unless it has suddenly got worse, or they ask to be seen same-day, or they report an emergency symptom. Long-standing pain that is bad by the end of the day is level 2.
- 2 (Ongoing) - recurring issue, worsening condition, injury past the acute phase, follow-up on existing treatment, condition affecting quality of life but not immediately disabling
- 1 (General) - information request, first-time enquiry with no urgency signal, exploring options, scheduling a routine appointment

Level 2 requires at least one of: the condition is getting worse, it stops the patient doing something they need to do, a clinician has already flagged it, or the message reports a warning sign. A clinician having flagged it is sufficient on its own: any doctor, GP, surgeon or other clinician who has noticed the problem, referred the patient here, or told them to get it looked at makes the case level 2, even when the patient reports no pain, no impairment and no concern of their own. Warning signs count even when the patient is otherwise functioning normally, and include unexplained weight loss, pain that wakes them at night, numbness, weakness, or a fall.

A patient who describes a symptom but has none of the above, is functioning normally, and wants maintenance, performance or general wellbeing is level 1 even though a symptom is present.

The patient's own view of how urgent it is never lowers the level. Treat "nothing urgent", "probably nothing", "no rush" and "I don't want to make a fuss" as absent - they are not clinical evidence. Symptoms this clinic cannot treat are still scored for urgency.

COMPLEXITY
- "high" - multiple symptoms, long duration (3+ weeks), prior treatment that failed, post-surgical, or multiple services potentially involved
- "medium" - single clear issue, moderate duration (1-3 weeks), or some relevant history
- "low" - new simple enquiry, no prior treatment mentioned, single straightforward concern

Complexity describes how much history the case carries, not how urgent it is. Score it independently of the urgency level.

RECOMMENDED ACTION
Write a plain English instruction for a non-medical receptionist. Be specific and actionable. Include the urgency reason. Write one sentence, or two at the most - never three, even when the case is complicated.

CONFIDENCE FLAG
Set to true if the message is too vague to classify reliably.

Return this exact JSON structure:
{{
  "service_category": "",
  "urgency_level": 1,
  "complexity": "",
  "recommended_action": "",
  "confidence_flag": false
}}

Patient message:
{message}"""


# Matches the workflow's Deepseek API node. Sending no temperature at all — which is what
# the workflow did until 2026-08-06 — measured 10/20 cases unstable across 5 runs; at 0 the
# same prompt measured 6/20. Override with --temp N to reproduce that.
TEMPERATURE = 0


def classify(catalog, message):
    """Call the model, then apply the workflow's own validation. Returns (result, gate)."""
    payload = {"model": "deepseek-v4-flash",
               "messages": [{"role": "user", "content": build_prompt(catalog, message)}]}
    if TEMPERATURE is not None:
        payload["temperature"] = TEMPERATURE
    body = post("https://api.deepseek.com/chat/completions", payload,
                {"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]})
    txt = body["choices"][0]["message"]["content"]
    cleaned = re.sub(r"```(json)?", "", txt).strip()

    # --- mirror of Parse Deepseek Response ---
    try:
        ai = json.loads(cleaned)
    except json.JSONDecodeError:
        return None, f"threw: bad JSON from model: {cleaned[:120]}"
    for k in ("service_category", "urgency_level", "complexity",
              "recommended_action", "confidence_flag"):
        if k not in ai:
            return None, f"threw: missing field {k}"
    if ai["urgency_level"] not in (1, 2, 3):
        return None, f"threw: invalid urgency_level {ai['urgency_level']!r}"

    names = [s["name"] for s in catalog]
    fallback = next((s["name"] for s in catalog if not s["assignable"]), None)
    degraded = []
    if ai["service_category"] not in names:
        degraded.append(f"service_category {ai['service_category']!r} -> {fallback}")
        ai["service_category"] = fallback
    if ai["complexity"] not in ("high", "medium", "low"):
        degraded.append(f"complexity {ai['complexity']!r} -> medium")
        ai["complexity"] = "medium"
    if not isinstance(ai["confidence_flag"], bool):
        degraded.append("confidence_flag -> true")
        ai["confidence_flag"] = True
    if degraded:
        ai["confidence_flag"] = True
    # A case nobody can be assigned to always needs review. Enforced here rather than
    # asked for in the prompt: the model was flipping this flag between runs on the same
    # input, and a rule the code applies cannot flip.
    if ai["service_category"] == fallback:
        ai["confidence_flag"] = True
    ai["needs_review"] = ai.pop("confidence_flag")
    return ai, ("degraded: " + "; ".join(degraded) if degraded else "clean")


def shape(g):
    """The decision, without the free text — this is what must be stable run to run."""
    return (f"{g['service_category']}/u{g['urgency_level']}/{g['complexity']}"
            f"/review={str(g['needs_review']).lower()}")


def check(case, got, catalog):
    """Returns a list of failure strings. Empty list means the case passed."""
    fails = []
    action = (got.get("recommended_action") or "").strip()

    # structural criteria, applied to every case
    if not action:
        fails.append("recommended_action is empty")
    if len(action) > 400:
        fails.append(f"recommended_action is {len(action)} chars, limit 400")
    sentences = len([s for s in re.split(r"[.!?]+", action) if s.strip()])
    if sentences > 2:
        fails.append(f"recommended_action is {sentences} sentences, prompt says max 2")
    if action.lower().startswith(GREETINGS):
        fails.append("recommended_action opens with a greeting; it is written for staff")
    if got["service_category"] not in [s["name"] for s in catalog]:
        fails.append(f"service_category {got['service_category']!r} is not in the catalog")

    for bad in case.get("recommended_action_must_not_contain", []):
        if bad.lower() in action.lower():
            fails.append(f"recommended_action leaked prompt text: {bad!r}")

    # per-case expectations
    for field, allowed in case.get("expect", {}).items():
        if got.get(field) not in allowed:
            fails.append(f"{field}: got {got.get(field)!r}, expected one of {allowed}")
    return fails


def main():
    load_env()
    catalog = fetch_catalog()

    if "--check-catalog" in sys.argv:
        return check_catalog(catalog)


    global TEMPERATURE
    repeat, positional, skip, which = 1, [], False, "cases"
    for i, a in enumerate(sys.argv[1:]):
        if skip:
            skip = False
            continue
        if a.startswith("--repeat"):
            if "=" in a:
                repeat = int(a.split("=", 1)[1])
            else:
                repeat = int(sys.argv[i + 2])
                skip = True
        elif a.startswith("--temp"):
            if "=" in a:
                TEMPERATURE = float(a.split("=", 1)[1])
            else:
                TEMPERATURE = float(sys.argv[i + 2])
                skip = True
        elif a.startswith("--set"):
            if "=" in a:
                which = a.split("=", 1)[1]
            else:
                which = sys.argv[i + 2]
                skip = True
        elif not a.startswith("-"):
            positional.append(a)

    f = HERE / ("cases.json" if which == "cases" else f"cases_{which}.json")
    if not f.exists():
        sys.exit(f"No such case file: {f.name}")
    spec = json.loads(f.read_text())
    cases = spec["cases"]

    if positional:
        cases = [c for c in cases if positional[0] in c["id"]]
        if not cases:
            sys.exit(f"No case id contains {positional[0]!r}")
    temp_note = "default (no temperature sent)" if TEMPERATURE is None else TEMPERATURE
    print(f"catalog: {', '.join(s['name'] for s in catalog)}")
    print(f"set:     {f.name}")
    print(f"running {len(cases)} case(s) x{repeat} against deepseek-v4-flash, temperature={temp_note}\n")

    def run_once(c):
        try:
            got, gate = classify(catalog, c["message"])
        except Exception as e:
            return None, f"error: {type(e).__name__}: {e}", ["classifier call failed"]
        if got is None:
            return None, gate, [gate]
        return got, gate, check(c, got, catalog)

    def run_one(c):
        """Runs a case `repeat` times. Passes only if every attempt passes."""
        attempts = [run_once(c) for _ in range(repeat)]
        shapes = {shape(g) for g, _, _ in attempts if g}
        fails = [f for _, _, fs in attempts for f in fs]
        return c, attempts, shapes, fails

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for r in pool.map(run_one, cases):
            results.append(r)

    passed = unstable = 0
    for c, attempts, shapes, fails in results:
        ok = not fails
        varies = len(shapes) > 1
        passed += ok
        unstable += varies
        mark = ("PASS" if ok else "FAIL") + ("·VAR" if varies else "")
        got, gate, _ = attempts[0]
        summary = shape(got) if got else gate
        print(f"[{mark:<8}] {c['id']:<26} {summary}")
        if varies:
            counts = {}
            for g, _, _ in attempts:
                if g:
                    counts[shape(g)] = counts.get(shape(g), 0) + 1
            spread = ", ".join(f"{k} x{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
            print(f"           unstable across {repeat} runs: {spread}")
        if gate != "clean" and got:
            print(f"           gate: {gate}")
        for f in dict.fromkeys(fails):
            print(f"           - {f}")

    total = len(results)
    print(f"\n{passed}/{total} passed, {total - passed} failed")
    if repeat > 1:
        print(f"{unstable}/{total} gave more than one distinct answer across {repeat} runs")
    elif unstable == 0:
        print("stability not measured — a single run per case. Use --repeat N.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
