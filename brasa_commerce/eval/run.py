#!/usr/bin/env python3
"""
Brasa Commerce — support classifier and escalation evaluation.

Runs every case in cases.json against the live DeepSeek classifier, applies the
same validation and the same escalation rules the n8n workflow applies, and
reports how many cases passed.

    python3 run.py                      # all cases, one run each
    python3 run.py order                # only cases whose id contains "order"
    python3 run.py --repeat 5           # five runs per case, reporting which answers vary
    python3 run.py --check-retrieval    # ask whether search_products can find every
                                        #   catalog product; no model calls, no cost

No rows are written, no emails are sent, no order is created. What it exercises is
the part of this system that can be *wrong* rather than merely *fail*: the intent
classification, and the decision about whether a human sees the reply before the
customer does.

Exit code is 0 only if every case passed.

What is asserted, per case
--------------------------
`intent`            a list of acceptable answers. One element is an exact
                    assertion; more than one marks a case where more than one
                    reading is genuinely defensible, and that case's `note` says
                    why. Absent means intent is not the property under test.
`escalate`          whether the ticket must be held for a human.
`reasons_include`   the escalation must fire for THIS reason. Without it a case
                    can pass by escalating for an unrelated one, which is how a
                    suite goes green while the rule it was written for is dead.
`reasons_exclude`   this reason must NOT fire. Used for the regression cases and
                    for the counter-directions.
`reply_must_not_contain`  substrings that must be absent from draft_reply, which
                    is the only model-authored prose that reaches a customer.

Credentials
-----------
Needs brasa_commerce/eval/.env (gitignored). One secret:

    DEEPSEEK_API_KEY=...

Retrieval uses the Supabase **anon** key — the same public key the storefront
serves in its own HTML — so it is not a secret and the script will scrape it from
the live site if you do not set SUPABASE_ANON_KEY. Nothing here can read a ticket,
a customer or an order: the anon key's entire reach is the active catalog.

KNOWN LIMITS — read these before trusting a green run.

1. The prompt below is a copy of the one `Build DeepSeek Body` builds, and the
   scoring is a copy of `Parse DeepSeek Response`. They can drift. The retrieved
   product context cannot drift — it is fetched live from search_products at run
   time — but the fixed text and the escalation rules are duplicated, and a change
   to either node that is not mirrored here will make this suite pass while
   production behaves differently.

2. Order context is a fixture, not a live lookup. search_order requires the
   service role, and this runner deliberately holds no privileged credential. So
   `needs_fulfilment_lookup` is exercised against a supplied order rather than
   against retrieval. Item 02 verified the live function separately.

3. A single green run says the answers were acceptable once. It says nothing
   about whether the same message gets the same answer twice. Use --repeat.
"""

import argparse, json, os, pathlib, re, statistics, sys, time
import urllib.request, urllib.error
import concurrent.futures

HERE = pathlib.Path(__file__).parent
MODEL = "deepseek-v4-flash"   # --model overrides, for the item-11 comparison
SUPABASE = "https://mjfvjogsknnuizsoygpp.supabase.co/rest/v1"
STOREFRONT = "https://brasacommerce-web.netlify.app"

VALID_INTENTS = ["order_status", "return_request", "product_question", "complaint", "other"]

# Copied verbatim from Parse DeepSeek Response. 'sue'/'suing' are deliberately
# absent: they matched the given name Sue and escalated a happy customer.
RISK_TERMS = re.compile(
    r"\b(lawyer|attorney|chargeback|charge-?back|fraud|fraudulent|lawsuit|litigation"
    r"|legal action|small claims|better business bureau|BBB)\b", re.I)
# Added 2026-08-07 after this suite caught an injection the model obeyed.
INJECTION_TERMS = re.compile(
    r"(?:ignore|disregard)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)"
    r"\s+(?:instruction|prompt|rule)|system\s+(?:instruction|prompt|message)"
    r"|escalate_flag|draft_reply|context_used"
    r"|\bintent\s*(?:to|=|:)\s*[\"']?(?:order_status|return_request|product_question|complaint|other)\b"
    r"|you\s+are\s+now\s+", re.I)
# Added 2026-08-07. The counterpart of `complaint`: that rule reads the model's own
# intent, so a message describing a broken product auto-sends whenever the model
# calls it anything else. The lookahead keeps culinary 'cracked pepper' out.
DAMAGE_TERMS = re.compile(
    r"\b(?:arrived|came|turned up|showed up)\s+(?:\w+\s+){0,2}"
    r"(?:broken|damaged|cracked|chipped|shattered|smashed|dented|bent|warped|defective|faulty|scratched)\b"
    r"|\bcrack(?:ed|s|ing)?\b(?!\s+(?:pepper|egg|eggs|corn))"
    r"|\b(?:shattered|smashed|chipped|dented|warped|snapped|defective|faulty|unusable)\b"
    r"|\bcame\s+(?:loose|apart|off)\b|\bfell\s+(?:apart|off)\b"
    r"|\bstopped\s+working\b|\bdoes\s*n(?:o|')?t\s+work\b", re.I)
SKU_IN_TEXT = re.compile(r"\bBC-[A-Z]{2,4}-\d{1,3}\b")

SAMPLES = []   # one entry per model call: latency and tokens, for item 11


# ── plumbing ────────────────────────────────────────────────────────────────
def load_env():
    """Reads .env into os.environ. DEEPSEEK_API_KEY is used in exactly one place —
    the Authorization header in classify() — and is never printed or written."""
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    if not os.environ.get("DEEPSEEK_API_KEY"):
        sys.exit("Missing DEEPSEEK_API_KEY. Put it in brasa_commerce/eval/.env — see the "
                 "docstring at the top of this file.")


def anon_key():
    k = os.environ.get("SUPABASE_ANON_KEY")
    if k:
        return k
    # The storefront publishes it; scraping it is the point, not a workaround.
    with urllib.request.urlopen(STOREFRONT, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", html)
    if not m:
        sys.exit("Could not read the anon key from the storefront. Set SUPABASE_ANON_KEY in .env.")
    return m.group(0)


def post(url, payload, headers, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ── the two nodes, reimplemented ────────────────────────────────────────────
def retrieve_products(message, key):
    """Mirrors the Retrieve Products node. Live call to the same function."""
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    try:
        out = post(f"{SUPABASE}/rpc/search_products", {"query_text": message}, h, timeout=30)
    except urllib.error.HTTPError as e:
        sys.exit(f"search_products failed: HTTP {e.code} {e.read().decode()[:200]}")
    row = out[0] if isinstance(out, list) and out else out
    return (row or {}).get("products") or []


def build_prompt(case, products):
    """Mirrors Build DeepSeek Body."""
    if products:
        context_block = "\n".join(
            f"- {p['sku']} — {p['product_name']} ({p['category']}, ${p['price']})\n"
            f"  {p.get('description')}\n"
            f"  Compatibility: {p.get('compatibility_notes') or 'n/a'}" for p in products)
        context_used = ", ".join(p["sku"] for p in products)
    else:
        context_block = "No specific catalog product matched this message."
        context_used = "None"

    order = case.get("order")
    if order:
        sh = order.get("shipment") or {}
        items = "; ".join(f"{i['qty']}× {i['product_name']} ({i['sku']})"
                          for i in order.get("items", [])) or "n/a"
        order_block = "\n".join([
            f"- Order {order['order_number']} — placed {order['placed_on']}, "
            f"status {order['status']}, total ${order['total']}",
            f"  Ships to: {order['ship_to']}",
            f"  Items: {items}",
            f"  Shipment: status {sh.get('status') or 'not created'}, "
            f"carrier {sh.get('carrier') or 'not assigned'}, "
            f"tracking {sh.get('tracking_number') or 'not issued'}, "
            f"ETA {sh.get('eta_date') or 'not set'}"])
    else:
        order_block = ("No order was matched for this customer. You do NOT have order data. "
                       "Do not state or estimate any shipping status, tracking number, "
                       "carrier or delivery date.")

    prompt = f"""You are a customer support AI for Brasa Commerce, a premium cookware brand in Chicago.
Your job: classify the intent of the customer message and draft a warm, helpful reply.

BRAND VOICE: Warm, knowledgeable, slightly artisanal. Not corporate. Treat customers as cooks who care about their tools.

RETRIEVED PRODUCT CONTEXT (ground every product detail ONLY in these facts — never invent specs, prices, or compatibility):
{context_block}

RETRIEVED ORDER CONTEXT (ground every shipping or order detail ONLY in these facts):
{order_block}

INTENT CATEGORIES:
- order_status: asking about shipping, delivery, tracking, or order processing
- return_request: wants to return, exchange, or get a refund
- product_question: asking about product specs, compatibility, usage, care, or comparisons
- complaint: expressing frustration, disappointment, or reporting damage/defects
- other: anything that does not fit above

ESCALATION RULES — set escalate_flag to true if:
- Intent is complaint
- Message contains words like lawyer, chargeback, fraud, lawsuit, BBB
- Confidence is below 0.70
- The situation requires checking an actual order in a fulfillment system

CUSTOMER MESSAGE:
Name: {case.get('customer_name') or 'Customer'}
Message: {case['message']}

Respond ONLY with valid JSON, no markdown, no backticks, no explanation:
{{
  "intent": "order_status|return_request|product_question|complaint|other",
  "draft_reply": "full reply addressed to the customer by first name, 3-5 sentences, no placeholder brackets",
  "confidence": 0.00,
  "escalate_flag": false,
  "context_used": "{context_used}"
}}"""
    return prompt, context_used


def classify(prompt):
    t0 = time.time()
    out = post("https://api.deepseek.com/chat/completions",
               {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                # Mirrors the cap on the production node, added at item 11. The answer
                # is a small JSON object; anything approaching this is a runaway, and a
                # truncated reply fails the JSON parse and escalates rather than sending.
                "max_tokens": 3000},
               {"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"})
    u = out.get("usage") or {}
    # The cache split is captured, not assumed. It is the largest single lever on
    # cost, and for a RAG prompt it cannot be guessed: only the fixed instruction
    # block is a stable prefix, and the retrieved product context in front of the
    # customer's message changes what is cacheable. Item 11 measures it.
    SAMPLES.append({"ms": int((time.time() - t0) * 1000),
                    "prompt_tokens": u.get("prompt_tokens"),
                    "completion_tokens": u.get("completion_tokens"),
                    "cache_hit": u.get("prompt_cache_hit_tokens"),
                    "cache_miss": u.get("prompt_cache_miss_tokens")})
    return out["choices"][0]["message"]["content"]


def score(text, case, context_used):
    """Mirrors Parse DeepSeek Response, including every escalation reason."""
    parse_failed = False
    try:
        parsed = json.loads(text.replace("```json", "").replace("```", "").strip())
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
    except Exception:
        parse_failed = True
        parsed = {"intent": "other", "draft_reply": "…", "confidence": 0.50, "escalate_flag": True}

    required = ["intent", "draft_reply", "confidence", "escalate_flag"]
    missing = [] if parse_failed else [k for k in required if parsed.get(k) is None]

    intent_ok = parsed.get("intent") in VALID_INTENTS
    intent = parsed["intent"] if intent_ok else "other"

    try:
        raw_conf = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        raw_conf = float("nan")
    conf_ok = raw_conf == raw_conf and 0.0 <= raw_conf <= 1.0
    confidence = max(0.0, min(1.0, raw_conf)) if raw_conf == raw_conf else 0.5

    draft = str(parsed.get("draft_reply") or "")
    retrieved = [] if context_used == "None" else [s.strip().upper() for s in context_used.split(",") if s.strip()]
    ungrounded = sorted({s for s in SKU_IN_TEXT.findall(draft.upper()) if s not in retrieved})
    order_found = bool(case.get("order"))

    reasons = []
    if parse_failed:                     reasons.append("parse_failed")
    if missing:                          reasons.append("contract_missing:" + "/".join(missing))
    if not parse_failed and not intent_ok: reasons.append("contract_intent:" + str(parsed.get("intent"))[:24])
    if not parse_failed and not conf_ok:   reasons.append("contract_confidence:" + str(parsed.get("confidence"))[:12])
    if parsed.get("escalate_flag") is True: reasons.append("model_flag")
    if confidence < 0.70:                reasons.append("low_confidence")
    if intent == "complaint":            reasons.append("complaint")
    if DAMAGE_TERMS.search(case["message"]): reasons.append("product_damage")
    if RISK_TERMS.search(case["message"]): reasons.append("risk_terms")
    if INJECTION_TERMS.search(case["message"]): reasons.append("injection_attempt")
    if ungrounded:                       reasons.append("ungrounded_sku:" + "/".join(ungrounded))
    if not draft.strip():                reasons.append("empty_draft")
    if len(re.findall(r"[^\W_]", case["message"], re.UNICODE)) < 10: reasons.append("no_substance")
    if intent == "order_status" and not order_found: reasons.append("needs_fulfilment_lookup")

    return {"intent": intent, "confidence": confidence, "draft_reply": draft,
            "escalate": bool(reasons), "reasons": reasons}


# ── assertions ──────────────────────────────────────────────────────────────
def check(case, got):
    exp, fails = case["expect"], []
    heads = [r.split(":", 1)[0] for r in got["reasons"]]

    if "intent" in exp and got["intent"] not in exp["intent"]:
        fails.append(f"intent={got['intent']}, expected one of {exp['intent']}")
    if "escalate" in exp and got["escalate"] != exp["escalate"]:
        fails.append(f"escalate={got['escalate']}, expected {exp['escalate']}")
    for r in exp.get("reasons_include", []):
        if r not in heads:
            fails.append(f"missing reason {r!r} (got {heads or ['none']})")
    for r in exp.get("reasons_exclude", []):
        if r in heads:
            fails.append(f"unwanted reason {r!r}")
    for s in exp.get("reply_must_not_contain", []):
        if s.lower() in got["draft_reply"].lower():
            fails.append(f"draft_reply contains {s!r}")
    return fails


def run_case(case, key):
    products = retrieve_products(case["message"], key)
    prompt, context_used = build_prompt(case, products)
    got = score(classify(prompt), case, context_used)
    return got, check(case, got)


# ── retrieval coverage ──────────────────────────────────────────────────────
def check_retrieval(key):
    """Can search_products find each product from its own name? No model calls.

    This answers a question carried since item 02 -- whether retrieval covers the
    corpus it is asked about -- with evidence rather than assumption. It is a
    floor, not a ceiling: finding a product by its own name is the easiest
    possible query."""
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    with urllib.request.urlopen(urllib.request.Request(
            f"{SUPABASE}/bc_product_catalog?select=sku,product_name&active=eq.true",
            headers=h), timeout=30) as r:
        catalog = json.loads(r.read().decode())

    print(f"\nsearch_products against {len(catalog)} active products\n")
    bad = 0
    for p in sorted(catalog, key=lambda x: x["sku"]):
        hits = [x["sku"] for x in retrieve_products(p["product_name"], key)]
        rank = hits.index(p["sku"]) + 1 if p["sku"] in hits else None
        ok = rank is not None
        bad += not ok
        print(f"  {'ok ' if ok else 'MISS'}  {p['sku']:<11} rank "
              f"{rank if rank else '—':<3} of {len(hits)}   ← {p['product_name']}")
    print(f"\n{len(catalog) - bad}/{len(catalog)} products retrievable by their own name")
    return 0 if bad == 0 else 1


# ── main ────────────────────────────────────────────────────────────────────
def main():
    global MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="?", default="")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--check-retrieval", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    # Item 11 compares tiers on the SAME frozen set. The alternative has to be
    # scored by the same code, or the comparison measures the harness.
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--set", choices=["main", "holdout"], default="main")
    args = ap.parse_args()

    MODEL = args.model

    key = anon_key()
    if args.check_retrieval:
        return check_retrieval(key)

    load_env()
    # The holdout set was written AFTER cases.json was frozen and was never used to
    # tune anything. Three of the escalation rules were written from cases in the main
    # set, so a green run there is partly a measure of fit to the cases that produced
    # the rules; this set targets the rules instead. See its __ABOUT__ entry.
    src = HERE / ("cases_holdout.json" if args.set == "holdout" else "cases.json")
    cases = [c for c in json.loads(src.read_text())
             if args.filter in c["id"] and not c["message"].startswith("__ABOUT__")]
    if not cases:
        sys.exit(f"No case id contains {args.filter!r}")

    print(f"{len(cases)} cases ({args.set}) × {args.repeat} run(s) · model {MODEL}\n")
    t0 = time.time()
    results = {c["id"]: [] for c in cases}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_case, c, key): c for c in cases for _ in range(args.repeat)}
        for f in concurrent.futures.as_completed(futs):
            c = futs[f]
            try:
                results[c["id"]].append(f.result())
            except Exception as e:
                results[c["id"]].append(({"intent": "?", "reasons": [], "escalate": None,
                                          "draft_reply": ""}, [f"runner error: {e}"]))

    passed = 0
    for c in cases:
        runs = results[c["id"]]
        ok = all(not fails for _, fails in runs)
        passed += ok
        answers = {(g["intent"], g["escalate"]) for g, _ in runs}
        flap = "  [VARIES]" if len(answers) > 1 else ""
        got, fails = runs[0]
        print(f"{'PASS' if ok else 'FAIL'}  {c['id']:<32} "
              f"{got['intent']:<17} conf {got['confidence']:.2f}  "
              f"{'escalate' if got['escalate'] else 'auto-send':<9} "
              f"{','.join(got['reasons']) or 'none'}{flap}")
        if not ok:
            for _, fs in runs:
                for msg in fs:
                    print(f"        · {msg}")

    varies = [c["id"] for c in cases if len({(g["intent"], g["escalate"]) for g, _ in results[c["id"]]}) > 1]
    print(f"\n{passed}/{len(cases)} cases passed in {time.time() - t0:.1f}s")
    if args.repeat > 1:
        print(f"{len(varies)} case(s) gave more than one answer across {args.repeat} runs"
              + (": " + ", ".join(varies) if varies else ""))
    else:
        print("Single run per case — this says nothing about stability. Use --repeat.")

    if SAMPLES:
        ms = sorted(s["ms"] for s in SAMPLES)
        pt = [s["prompt_tokens"] for s in SAMPLES if s["prompt_tokens"]]
        ct = [s["completion_tokens"] for s in SAMPLES if s["completion_tokens"]]
        if pt and ct:
            print(f"model calls: n={len(ms)}  p50 {statistics.median(ms):.0f}ms  "
                  f"max {ms[-1]}ms  avg tokens {statistics.mean(pt):.0f} in / "
                  f"{statistics.mean(ct):.0f} out")
        hit = [s["cache_hit"] for s in SAMPLES if s["cache_hit"] is not None]
        mis = [s["cache_miss"] for s in SAMPLES if s["cache_miss"] is not None]
        if hit and mis and (sum(hit) + sum(mis)):
            print(f"prompt cache: {sum(hit)} hit / {sum(mis)} miss  "
                  f"= {100 * sum(hit) / (sum(hit) + sum(mis)):.0f}% of input tokens served from cache")

    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
