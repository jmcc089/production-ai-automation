# Production AI Automation

Three AI intake-automation builds sharing one method: read an unstructured message from a
member of the public, classify it, route it by urgency and service, and refuse to write anything
that fails a contract.

| Build | Domain | What is distinct about it |
|---|---|---|
| `cedar_healthcare/` | Clinic intake triage | Classify and route only — the system never gives clinical advice. Acute cases short-circuit to an urgent alert. |
| `holt_vargas/` | Law firm intake | Grounded on an in-prompt checklist rather than retrieval. |
| `brasa_commerce/` | E-commerce support | True RAG over a product and order corpus. |
| `n8n/` | Shared automation runtime | Build source for the Railway service the three workflows run on. |

Each build is a static dashboard plus an n8n workflow. All three share one Supabase project and one
n8n instance, isolated by table prefix (`ch_`, `hvl_`, `bc_`) and by webhook path.

**The full case study, architecture decisions and screenshots live in Notion.** This repository is
the code and the operational documentation; it is not the write-up.

---

## What you need before anything runs

Nothing here works standalone — every build depends on four external services. There is no
docker-compose that brings the world up locally, and pretending otherwise would waste your first
hour.

| Service | Used for | Free tier sufficient |
|---|---|---|
| Supabase | Postgres, auth, RLS | yes |
| n8n (self-hosted on Railway) | the workflows | Railway is paid; n8n itself is free |
| Deepseek | classification | pay per call, cents |
| Resend | transactional email | yes, but sending to arbitrary addresses needs a verified domain |
| Netlify | the dashboards | yes |
| Tally | the public intake form | yes |

---

## Running a dashboard locally

The dashboards are single-file HTML with no framework and no bundler. The "build" is two string
substitutions.

```bash
cd cedar_healthcare/app
export SUPABASE_URL="https://<your-project>.supabase.co"
export SUPABASE_KEY="<your anon key>"     # anon, never service_role
npm run build                              # rewrites index.html in place
python3 -m http.server 8000                # then open http://localhost:8000
```

`npm run build` **modifies `index.html` in place**, replacing `__SUPABASE_URL__` and
`__SUPABASE_KEY__`. Do not commit the result — `git checkout index.html` afterwards. This is
deliberate: the repository holds placeholders so no key is ever committed, and Netlify substitutes
them at deploy time from its own environment variables.

You will see a login screen. Access requires a Supabase Auth user whose id appears in
`ch_practitioners.auth_user_id`; there is no local bypass, because the row-level security policies
are the access control and mocking them would test something other than the system.

## Bringing one up from zero

Ordered, and each step is needed by the next.

1. **Supabase project.** Run the migrations in order. They are listed under *Database → Migrations*
   in an existing project; for Cedar the sequence starts at
   `move_practitioner_from_patients_to_intake_requests` and ends at
   `ch_drop_duplicate_assignment_trigger`.
2. **Seed the reference tables.** `ch_services` must contain at least one row with
   `assignable = false` — the classifier degrades to it and a foreign key depends on it. Then
   `ch_practitioners`, whose `role` must match a `ch_services.name` exactly, because that equality
   *is* the routing rule.
3. **Auth users.** Create one Supabase Auth user per practitioner and write its uuid into
   `ch_practitioners.auth_user_id`. Until you do, that person signs in successfully and sees an
   empty dashboard.
4. **n8n on Railway.** Deploy `n8n/` as a Dockerfile service with a volume and an attached Postgres.
   Set `N8N_ENCRYPTION_KEY` before creating any credential.
5. **Workflow env vars**, as Railway service variables — the workflow reads them as `{{ $env.NAME }}`:
   `SUPABASE_SERVICE_KEY`, `DEEPSEEK_API_KEY`, `RESEND_API_KEY`.
6. **Import and publish the workflow.** Publishing is separate from saving; the running system uses
   the published version.
7. **Resend domain.** Verify a sending domain and press *Verify DNS Records* — publishing the DNS is
   not enough, and this has caused a real outage (see `cedar_healthcare/INCIDENTS.md`).
8. **Tally form.** Point its webhook at `/webhook/cedar-intake`. The four question labels must read
   exactly `Full name`, `Email address`, `Phone number`, `Tell us about your reason for visit` —
   the workflow matches on label text.
9. **Netlify.** One project per `app/` directory, with `SUPABASE_URL` and `SUPABASE_KEY` set.

Verify with the health check in `cedar_healthcare/RUNBOOK.md`.

**Not rehearsed end to end.** These steps are reconstructed from the built system and from the
migration and deployment record, not from a clean-room rebuild. Steps 1–3 and 5 have been verified
against what the code actually reads; the ordering of 4 and 6–9 is derived rather than re-executed.
Treat gaps as likely and report them.

---

## Cedar Healthcare, in more detail

The build this repository documents most thoroughly.

- [`cedar_healthcare/RUNBOOK.md`](cedar_healthcare/RUNBOOK.md) — health check, failure triage, stop
  and start, key rotation, adding a service.
- [`cedar_healthcare/INCIDENTS.md`](cedar_healthcare/INCIDENTS.md) — two write-ups, including a
  routing failure that ran in production for two weeks and the regression its fix caused.
- [`cedar_healthcare/SECURITY.md`](cedar_healthcare/SECURITY.md) — what is reachable without
  credentials, what refuses it, and five open gaps with the cost of closing each.

---

## A note on this repository's history

The first commit here contains all six applications already built: this monorepo was assembled on
2026-07-16 from six separate repositories, and the development history of each build lives in the
repository it came from. The middle of that period is also thin — a run of commits reading
`Update index.html` and `Update package.json`, made through GitHub's web editor during a hosting
migration, which record that something changed but not what or why.

Commits from 2026-08-01 onward are what the rest of this repository should be judged against: they
name a change and its reason. History cannot be back-dated, so this is recorded rather than
disguised.
