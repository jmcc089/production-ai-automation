#!/usr/bin/env python3
"""
Brasa Commerce — does a real customer actually receive the email?

    python3 sender_probe.py                 # fire one intake, print what to check
    python3 sender_probe.py --verify        # ...and check it, if the keys are in env
    python3 sender_probe.py --dry-run       # print the payload, send nothing

THIS HITS PRODUCTION AND HAS SIDE EFFECTS: one bc_customers row, one
bc_support_tickets row, one bc_workflow_runs row, and one email through Resend.
Clean-up SQL is printed at the end.

Why this exists
---------------
Until 2026-08-07 Brasa sent from Resend's sandbox sender, `onboarding@resend.dev`,
which delivers ONLY to the Resend account owner's exact address. Every
customer-facing send in the build failed for every real recipient, while the
ticket recorded `sent` and the caller was told 200. That is closed - the sender
is now the verified domain - but the closing run could not be made on the day,
because the assurance pass had exhausted Resend's daily quota by then.

So the property this script exists to prove is narrow and specific:

    an address that is NOT the account owner's exact address receives the mail.

A plus-alias is the right probe for that. It is the owner's own mailbox, so
nothing reaches a third party, and it is exactly what the sandbox sender used to
refuse: `You can only send testing emails to your own email address`.

Reading the result
------------------
The interesting failure is not "no email". It is any of these:

  * The run errors at `Send Customer Email` with a 403 mentioning testing
    addresses -> the sender regressed to the sandbox. The finding is back.
  * The run errors with `daily email sending quota` -> the quota, not the
    sender. Nothing is proved either way; come back later.
  * The email is accepted but `bounced` -> accepted for delivery, which for a
    third-party address is the proof; for a plus-alias of a real mailbox it is
    not, and means something else is wrong.
  * The ticket reads `sent` while no email exists -> the two-phase status write
    regressed. That is the defect this build had and the one worth watching.

A ticket left at `approved` after a failed send is CORRECT. It means the row is
telling the truth about a delivery that did not happen.

Credentials
-----------
Never hard-code them. Without them the script still fires the probe and prints
the two queries to run by hand; with them it reaches a verdict itself.

    export SUPABASE_SERVICE_KEY=...      # from the Supabase project settings
    export RESEND_API_KEY=...            # from resend.com/api-keys
"""

import argparse, json, os, sys, time, urllib.request, urllib.error

WEBHOOK  = "https://n8n-production-3503.up.railway.app/webhook/brasa-intake"
SUPABASE = "https://mjfvjogsknnuizsoygpp.supabase.co/rest/v1"
RESEND   = "https://api.resend.com/emails"

# The plus-alias goes to this same mailbox, so nothing reaches anyone else.
OWNER    = "mcruzcardoza@gmail.com"
EXPECTED_DOMAIN = "mail.mcruz-portfolio.space"

MESSAGE = ("Sender probe. Does the seasoning on the No.10 cast iron skillet need "
           "redoing after a dishwasher accident?")


def post(url, payload, headers=None, timeout=90):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
        return r.status, (json.loads(body) if body.strip() else None)


def get(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="check the database and Resend afterwards (needs the keys in env)")
    ap.add_argument("--wait", type=int, default=20,
                    help="seconds to wait before checking (default 20)")
    args = ap.parse_args()

    # Unique per run: the intake webhook is idempotent on this key, so a reused
    # one would be deduplicated and send no email at all - which would look like
    # a sender failure and is not one.
    key   = f"sender-probe-{int(time.time())}"
    local, domain = OWNER.split("@")
    alias = f"{local}+{key}@{domain}"

    payload = {
        "eventId": key, "eventType": "FORM_RESPONSE",
        "data": {"responseId": key, "formId": "javEX9", "fields": [
            {"key": "q1", "label": "Full name",     "type": "INPUT_TEXT",  "value": "Sender Probe"},
            {"key": "q2", "label": "Email address", "type": "INPUT_EMAIL", "value": alias},
            {"key": "q3", "label": "Phone number",  "type": "INPUT_PHONE_NUMBER", "value": "+1 312 555 0147"},
            {"key": "q4", "label": "Message",       "type": "TEXTAREA",    "value": MESSAGE},
        ]}}

    print(f"recipient : {alias}")
    print(f"key       : {key}")
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    try:
        status, body = post(WEBHOOK, payload)
    except urllib.error.URLError as e:
        print(f"FAIL  the webhook did not answer: {e}")
        return 2
    print(f"webhook   : HTTP {status} {json.dumps(body)}")

    if not body or not body.get("ticket_id"):
        print("FAIL  no ticket_id came back. Nothing further can be checked from here.")
        return 2
    ticket_id = body["ticket_id"]

    sql = (f"select id, status, updated_at from bc_support_tickets where id = '{ticket_id}';\n"
           f"select n8n_execution_id, status, stage, error_node, error_message\n"
           f"  from bc_workflow_runs where source_event_id = '{key}';")

    if not args.verify:
        print(f"\nWait ~{args.wait}s, then check:\n\n{sql}\n")
        print(f"and in Resend, the most recent message to {alias}.")
        print(f"\nClean up:\n  delete from bc_support_tickets where source_event_id = '{key}';"
              f"\n  delete from bc_customers where email = '{alias}';")
        return 0

    sb_key = os.environ.get("SUPABASE_SERVICE_KEY")
    rs_key = os.environ.get("RESEND_API_KEY")
    if not sb_key or not rs_key:
        print("\n--verify needs SUPABASE_SERVICE_KEY and RESEND_API_KEY in the environment.")
        print(f"Falling back to the manual check:\n\n{sql}")
        return 1

    print(f"waiting {args.wait}s for the run to finish…")
    time.sleep(args.wait)

    hdr = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
    ticket = get(f"{SUPABASE}/bc_support_tickets?id=eq.{ticket_id}&select=status", hdr)
    runs   = get(f"{SUPABASE}/bc_workflow_runs?source_event_id=eq.{key}"
                 f"&select=n8n_execution_id,status,stage,error_node,error_message", hdr)
    tstatus = ticket[0]["status"] if ticket else "(no row)"
    run = runs[0] if runs else {}
    print(f"ticket    : status={tstatus}")
    print(f"run       : {json.dumps(run)}")

    mails = get(f"{RESEND}?limit=25", {"Authorization": f"Bearer {rs_key}"}).get("data", [])
    mine  = [m for m in mails if alias in json.dumps(m.get("to"))]

    print()
    if mine:
        m = mine[0]
        detail = get(f"{RESEND}/{m['id']}", {"Authorization": f"Bearer {rs_key}"})
        frm, st = detail.get("from", ""), detail.get("last_event") or m.get("last_event")
        print(f"email     : from={frm} status={st}")
        if EXPECTED_DOMAIN not in frm:
            print(f"FAIL      the sender is not {EXPECTED_DOMAIN}. The sandbox sender is back.")
            return 2
        if tstatus != "sent":
            print(f"FAIL      the mail went out but the ticket reads '{tstatus}', not 'sent'.")
            return 2
        print("PASS      a non-owner address was accepted, the sender is the verified "
              "domain, and the ticket says 'sent' only because it was.")
        rc = 0
    else:
        err = (run.get("error_message") or "")
        if "quota" in err.lower():
            print("INCONCLUSIVE  the daily Resend quota is exhausted. Nothing is proved "
                  "about the sender either way. Re-run after it resets.")
            rc = 1
        elif "testing email" in err.lower() or "resend.dev" in err.lower():
            print("FAIL      the sandbox restriction is back. This is the original defect.")
            rc = 2
        else:
            print(f"FAIL      no email reached {alias}. Run says: {err or '(nothing recorded)'}")
            rc = 2
        if tstatus == "sent":
            print("FAIL      …and the ticket reads 'sent' anyway. The two-phase write regressed.")
            rc = 2
        elif tstatus == "approved":
            print("          The ticket reads 'approved', which is correct: it is not "
                  "claiming a delivery that did not happen.")

    print(f"\nClean up:\n  delete from bc_support_tickets where source_event_id = '{key}';"
          f"\n  delete from bc_customers where email = '{alias}';")
    return rc


if __name__ == "__main__":
    sys.exit(main())
