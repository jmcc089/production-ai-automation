# Production AI Automation: Three builds, one method

> The same automation pattern that saves a cookware brand a support headache would get a clinic sued. That's the whole point. These three builds start from one identical bottleneck — unstructured messages that a human has to read, judge, and route by hand — but each lands in an industry where "wrong" means something different: for a clinic, an acute case buried in a routine queue; for a law firm, a missing document surfaced too late to file; for a retailer, a chargeback threat answered by a bot. Same method, three different definitions of unacceptable, and the engineering judgment shows in how each one grounds, gates, and fails. Read the ADRs side by side and the tooling stops being the story.

Interactive brief: https://ai-solutions-brief.netlify.app

---

## 1. System Design

The thinking before the build: how each problem was taken from raw business pain to a validated system. Each case documents the same six things: the business, the problem, the requirements it produced, the architecture decisions (ADRs), the JSON contract, and the deliberate scope.

One method applied across three different industries, chosen to stress-test that method against different constraints: clinical urgency, legal completeness, and commercial brand voice. While the pattern holds across all three, the design decisions do not. The clearest tell is how each grounds their build: Cedar classifies with no retrieval, Holt & Vargas grounds on in-prompt checklists, and Brasa uses true retrieval (RAG). Read the ADRs side-by-side, and it becomes clear that engineering judgment is the point, not the tooling.

### 1.1 Cedar Healthcare

> Demo business. The system is real, deployed, and live; the company is simulated to frame the problem.

**About the business**
Cedar Healthcare is a wellness clinic in Austin, TX, running three service lines — Physiotherapy, Sports Massage, and Clinical Nutrition. It has clinical capacity but limited front-desk capacity, no in-house engineering (anything built must run hands-off), and a low-to-zero recurring budget that favors free-tier tooling. A hard clinical-safety boundary applies: the business may triage and route enquiries, but must never give medical advice to patients.

**Problem to solve**
Every patient enquiry arrives as unstructured free text. A staff member has to read it, infer which service it's for, judge how urgent it is, and route it by hand — one message at a time. Two costs follow. First, time: roughly 35 min/day of triage taken from patients physically in the clinic. Second, risk of slippage: with no consistent urgency signal, acute cases (active pain, post-surgical complications) sit in the same queue as routine enquiries — an estimated ~2 missed or late callbacks per month, which is a care-quality and reputational problem. The underlying issue is that triage judgment lives only in a person's head, is applied inconsistently, and leaves no structured record of *why* a case was prioritized.

*Baseline figures are working estimates for a clinic of this size, illustrative of the problem, not audited metrics.*

**Requirements**

Functional:
- Auto-classify every intake: service, urgency (1–3), complexity, recommended action — no human reading required to route.
- Urgency-3 (acute) triggers an immediate practitioner alert at submission.
- Each intake carries a structured, auditable urgency rationale + confidence flag.
- A dashboard lets staff work cases New → In Progress → Resolved, with a per-patient timeline.
- The patient-facing email stays a neutral confirmation only.

Non-functional / hard rules:
- Strict JSON contract; `urgency_level` validated ∈ {1,2,3}; hard-fail on malformed JSON rather than writing bad data to a clinical queue.
- Never give medical advice to the patient (clinical-safety boundary).
- Runs hands-off (no in-house engineering); free-tier friendly.

**Architecture Decisions Record (ADRs)**
- **ADR-1 — Classify-and-route, never auto-advise.** The AI triages and writes a recommended action for staff; the patient gets only a neutral confirmation. *Why:* automated medical advice is unacceptable liability for a clinic. *Rejected:* auto-replying to the patient with guidance.
- **ADR-2 — Three-level urgency with a hard acute short-circuit.** Urgency 1/2/3 with explicit acute criteria; urgency-3 branches immediately to a practitioner alert. *Why:* the core failure mode is acute cases sitting in an undifferentiated queue — this makes that structurally impossible. *Rejected:* a single "priority" boolean (too coarse for routing + dashboard sorting).
- **ADR-3 — Strict JSON contract + validation gate.** A validation node guards the DB write. *Why:* the database feeds a clinical work queue; silent bad data is worse than a visible failure. *Rejected:* trusting free-form output and regex-scraping it.
- **ADR-4 — Upsert-by-email patient identity.** `ch_patients` upserts on email, linking each intake to a stable patient id. *Why:* dedupes returning patients and enables a longitudinal intake timeline. *Rejected:* a new patient row per message (fragmented history).
- **ADR-5 — Single shared platform, namespaced.** Shares one Supabase project and one n8n instance with the other builds, isolated by the `ch_` prefix and a dedicated webhook path. *Why:* minimizes operational overhead across a multi-client portfolio. *Trade-off:* logical (not physical) isolation — revisited under RLS hardening.

**The JSON contract**
The model must emit JSON only — no prose, no code fences. Required keys: `service_category`, `urgency_level`, `complexity`, `recommended_action`, `confidence_flag`.

Classification rules (carried in the system prompt):
- **service_category** — Physiotherapy / Sports Massage / Clinical Nutrition / Unknown.
- **urgency_level** — 3 = acute (active pain, can't perform daily function, just-occurred injury, same-day request); 2 = ongoing / worsening / follow-up; 1 = general / routine.
- **complexity** — high / medium / low, by symptom count, duration, prior failed treatment.
- **recommended_action** — one plain-English instruction for a non-clinical receptionist, including the urgency reason.
- **confidence_flag** — true when the message is too vague to classify reliably.

A validation node strips fences, asserts required fields, and rejects any `urgency_level` outside (1,2,3) — a bad response fails loudly.

**Scope**
A scoped MVP by design: the smallest system that proves the pattern end-to-end, not a production deployment at scale.

Out of scope by choice: any clinical advice to patients · EHR integration · multi-language intake · the clinical-records/notes backbone (built but not yet surfaced).

Next column of work: move secrets to a credential store + rotate · scope RLS for patient data · surface the clinical-records backbone in the dashboard. These are the hardening steps, not the demonstration.

---

### 1.2 Holt & Vargas

> Demo business. The system is real, deployed, and live; the company is simulated to frame the problem.

**About the business**
Holt & Vargas is an immigration law firm handling four common case types — Family Visa, EB Green Card, Asylum, and Naturalization. New clients arrive with documents in varying states of completeness — often partial, sometimes not in English. The firm has a small team, no in-house engineering, a low recurring budget, and a hard boundary that legal judgment stays human-owned: the system may assess and flag, but a paralegal owns every case.

**Problem to solve**
The bottleneck isn't legal analysis — it's the repetitive, error-prone work of checking whether an intake packet contains everything a given case type requires before paralegal time is invested. Two costs follow. First, paralegal time goes to triage instead of casework: manually cross-referencing every intake against the right checklist is slow and scales badly with caseload. Second, incomplete packets get discovered late: when a missing critical document (an unsigned I-864, absent sponsor financials) surfaces deep into the process, it delays filing and frustrates clients at a moment when timelines carry legal weight. The underlying issue is that completeness judgment is applied manually and inconsistently, with no structured, auditable record of what was present, what was missing, and how complete the packet was at intake.

*Pain-point framing reflects working assumptions for a firm of this size — illustrative, not audited metrics.*

**Requirements**

Functional:
- Auto-score each intake for completeness against the correct case-type checklist.
- List documents present / missing / optional at intake.
- Store a completeness score, label, confidence, and priority flag per review.
- A dashboard shows the case queue with completeness stats and per-case detail.

Non-functional / hard rules:
- Strict JSON contract; relational client → case → review chain (not a flat table).
- Fail safe: if the AI can't analyze reliably, flag for manual review rather than guess — a malformed response routes to a human, priority-flagged.
- Multilingual documents supported at intake; legal judgment stays human-owned.
- Runs hands-off; low recurring budget.

**Architecture Decisions Record (ADRs)**
- **ADR-1 — Relational client → case → review chain, not a flat table.** Three linked tables with FKs and a uniqueness constraint on `(client_id, case_type)`. *Why:* mirrors the real domain — a client can have multiple case types over time, and re-submissions update the existing case rather than duplicate; also gives the dashboard clean joins and per-client history. *Rejected:* one flat reviews table (duplicates data, loses history).
- **ADR-2 — In-prompt checklists, not retrieval (RAG).** The four case-type checklists are embedded directly in the prompt. *Why:* the checklist set is small, stable, and fully known; static injection is simpler, cheaper, and more deterministic than a retrieval layer for content that rarely changes. *Rejected:* RAG over a requirements corpus (unnecessary at this scale; revisit if checklists grow).
- **ADR-3 — Score thresholds map AI output to an operational label.** The 0–100 score is bucketed at the persistence layer (≥75 complete / ≥40 review-needed / else incomplete). *Why:* gives the paralegal a consistent at-a-glance signal independent of the model's free-text label. *Rejected:* relying solely on the model's self-assigned label.
- **ADR-4 — Fail safe to a human on AI uncertainty.** On parse failure or low confidence, the case is priority-flagged and routed to manual review. *Why:* in a legal context, a silently wrong completeness assessment is worse than an explicit "needs a human." *Rejected:* dropping or auto-approving unparseable responses.

**The JSON contract**
JSON only. Keys: `completeness_score`, `completeness_label` (Complete / Mostly Complete / Incomplete / Critically Incomplete), `documents_present`, `documents_missing`, `documents_optional`, `ai_summary`, `ai_notes`, `priority_flag`, `confidence_level`.

Grounding: the required-documents checklist for all four case types is embedded in the system prompt; the model evaluates the client's description against the checklist for the submitted case type (in-prompt grounding, not retrieval — see ADR-2). On a parse error, a defensive fallback object is returned with `priority_flag=true` and a "manual review required" summary.

**Scope**
A scoped MVP by design: the smallest system that proves the pattern end-to-end, not a production deployment at scale.

Out of scope by choice: validating the *legal content* of documents (it checks presence vs checklist, not legal sufficiency) · the actual filing · document upload / OCR of real files · case-management integration (Docketwise/Clio).

Next column of work: scope RLS for client PII · add document upload + OCR · integrate case-management. These are the hardening steps, not the demonstration.

---

### 1.3 Brasa Commerce

> Demo business. The system is real, deployed, and live; the company is simulated to frame the problem.

**About the business**
Brasa Commerce is a premium cookware brand — cast iron, carbon steel, ceramic — with a focused catalog of roughly seven SKUs whose customers ask detailed, product-specific questions. Support volume is rising faster than the team can staff, and a large share of messages are repetitive product questions the catalog can already answer. The team is small, has no in-house engineering, a low recurring budget, and is brand-voice sensitive (warm, knowledgeable, never corporate-generic), with a hard rule that sensitive cases must reach a human before any reply goes out.

**Problem to solve**
Two costs. First, repetitive load: agents re-type similar replies to product-knowledge questions instead of focusing on cases that need judgment. Second, inconsistent triage and risk exposure: without a consistent process, an angry or legally-charged message (a chargeback threat, a damaged-product complaint) can be handled with the same priority as a routine question, and a wrong automated answer to a sensitive case would damage the brand. The underlying issue is that product knowledge and triage judgment are applied manually and inconsistently, with no mechanism to let automation handle the easy, well-grounded cases while reliably holding the risky ones for a human.

*Support-volume framing reflects working assumptions for a brand of this size — illustrative, not audited metrics.*

**Requirements**

Functional:
- Draft routine, confident replies automatically, grounded in real catalog facts (not invented specs).
- Hold complaints / low-confidence / risk-keyword messages for human approval before sending.
- A dashboard lets agents review, edit, approve-and-send, or escalate.
- Store intent, confidence, escalation flag, retrieved context, and status per ticket.

Non-functional / hard rules:
- Strict JSON contract; replies must cite actual retrieved catalog facts.
- No wrong answers on sensitive cases — complaints and low-confidence cases must reach a human before any customer email.
- Brand-voice consistency; runs hands-off; low recurring budget.

**Architecture Decisions Record (ADRs)**
- **ADR-1 — Postgres full-text search, not a vector database.** Grounding runs via a Postgres FTS `search_products` RPC over the ~7-SKU catalog. *Why:* at this catalog size, FTS gives accurate, explainable, zero-extra-infrastructure retrieval, reusing the database already in the stack; a vector DB adds cost and operational surface with no benefit at this scale. *Rejected:* embeddings + vector store (documented as the upgrade path once the catalog grows).
- **ADR-2 — Human-in-the-loop gate, not auto-send-everything.** Confident routine tickets auto-send; complaints and low-confidence tickets are held for agent approval. *Why:* the brand-risk cost of a wrong automated reply to a complaint is high; gating sensitive cases is the entire point. *Rejected:* auto-sending all replies with after-the-fact review.
- **ADR-3 — Separate Approve & Send webhook, not pausing the intake flow.** The held-ticket reply is sent by a second, independent webhook path when the agent clicks Approve. *Why:* approval happens asynchronously, by a different actor; webhook automations can't cleanly "wait for a human." *Rejected:* a long-running/paused intake execution awaiting approval (fragile).
- **ADR-4 — Deterministic escalation overrides the model.** Escalation is decided by explicit rules (intent, confidence threshold, risk keywords) in code, not the model's self-reported flag alone. *Why:* sensitive-case routing must be predictable and auditable, not subject to model variability. *Rejected:* trusting the model's `escalate_flag` unconditionally.

**The JSON contract**
JSON only. Keys: `intent`, `draft_reply`, `confidence`, `escalate_flag`, `context_used`.
- **intent** — order_status / return_request / product_question / complaint / other.
- **Retrieval** — a Postgres `search_products(query_text)` function runs FTS (OR-matched lexemes) over the catalog and returns top matches as a JSON array, injected into the prompt as a grounded context block; the retrieved SKUs are written to `context_used`.
- **Deterministic escalation** — the parse step overrides the model and forces `escalate_flag=true` on complaint intent, confidence < 0.70, or risk keywords (lawyer, chargeback, fraud, lawsuit, BBB); a fail-safe fallback handles parse errors by defaulting to escalation.

**Scope**
A scoped MVP by design: the smallest system that proves the pattern end-to-end, not a production deployment at scale.

Out of scope by choice: writing to an order system · payments · live order-status lookups · multi-language · vector/semantic retrieval (FTS is sufficient at this catalog size).

Next column of work: scope RLS for ticket/customer data · vector-search upgrade once the catalog outgrows FTS · live order-system lookups for order_status intents. These are the hardening steps, not the demonstration.

---

## 2. Build Evidence

What got built, and the proof it runs. Each case shows the live automation (the n8n workflow verified in the running instance), the Supabase data layer, the operator interface, and the end-to-end verification that exercised it. System Design is the *why*; this is the *what shipped*.

### 2.1 Cedar Healthcare

**The stack**

| Layer | Tool | Purpose |
|---|---|---|
| Intake form | Tally | Free-text patient enquiry → webhook |
| Orchestration | n8n (Railway) | Classify → validate → persist → route |
| AI | DeepSeek `deepseek-v4-flash` | JSON-only triage classification |
| Database | Supabase | PostgreSQL + RLS — patients + intake queue |
| Email | Resend | Practitioner alert + neutral patient confirmation |
| Frontend | static HTML/JS (Railway) | Marketing site + front-desk dashboard |

**The automation (n8n)**
Workflow **"Cedar Healthcare — Intake Triage"**, webhook `cedar-intake` — verified live in the running n8n instance with the validate-then-branch logic below.

```
Tally form (embedded in a client-facing website)
   │  POST /webhook/cedar-intake
   ▼
n8n (Railway)
   Set Fields  → normalize Tally fields (name, email, phone, reason-for-visit)
   Build prompt → inject classifier system prompt
   DeepSeek API → deepseek-v4-flash, JSON-only
   Parse + validate → required fields present, urgency ∈ {1,2,3}, hard-fail on bad JSON
   Upsert Patient → ch_patients (on_conflict=email) → extract id
   Insert Intake → ch_intake_requests (FK patient_id)
        ├─ IF urgency == 3 → Build urgent email → Resend (practitioner alert)
        └─ Always         → Build confirmation → Resend (neutral patient confirmation)
   ▼
Front-desk dashboard (Railway) ── reads/updates ──► Supabase
```

**The data layer (Supabase)**
Six tables form the clinical backbone; the intake pipeline writes to the first two.

| Table | Purpose |
|---|---|
| `ch_patients` | Identity (unique email), contact, assigned practitioner, status |
| `ch_intake_requests` | raw_message, service_category, urgency_level (CHECK 1/2/3), complexity, recommended_action, confidence_flag, status (default 'New'), patient_id FK |
| `ch_practitioners` | name, role, license, active |
| `ch_treatments` | service catalog (9 treatments across 3 lines) |
| `ch_clinical_records` / `ch_clinical_notes` | visit-level records & notes (backbone; not yet surfaced) |

**The interfaces**
- Marketing site (Tally intake form): [github](https://github.com/jmcc089/production-ai-automation/tree/main/cedar_healthcare/web) · [live](https://cedarhealthcare-web.up.railway.app/)
- Front-desk dashboard (urgency-sorted queue, New → In Progress → Resolved, per-patient timeline): [github](https://github.com/jmcc089/production-ai-automation/tree/main/cedar_healthcare/app) · [live](https://cedarhealthcare-app.up.railway.app/)

**QA & verification**
- End-to-end test matrix across acute / ongoing / routine for all 3 services, verifying DB rows + correct routing (11 seeded intakes).
- Urgent alert + patient confirmation render correctly across Gmail / Outlook / Apple Mail / mobile.
- Dashboard reflects live Supabase data with working status transitions and per-patient timeline.

---

### 2.2 Holt & Vargas

**The stack**

| Layer | Tool | Purpose |
|---|---|---|
| Intake form | Tally | Case-type + document description → webhook |
| Orchestration | n8n (Railway) | Checklist-grounded scoring → relational persist → route |
| AI | DeepSeek `deepseek-v4-flash` | JSON-only completeness scoring |
| Database | Supabase | PostgreSQL + RLS — relational client → case → review |
| Email | Resend | Paralegal report + client "what's still needed" |
| Frontend | static HTML/JS (Railway) | Marketing site + paralegal dashboard |

**The automation (n8n)**
Workflow **"Holt & Vargas — Document Intake & Completeness Review"**, webhook `hvl-intake` — verified live, with the relational upsert chain (client → case → review) and a `Respond to Webhook` node closing the request.

```
Tally form
   │  POST /webhook/hvl-intake
   ▼
n8n (Railway)
   Set Fields  → name, email, phone, case_type, document_description, country_of_origin
   Build prompt → inject the required-documents checklist for the submitted case type
   DeepSeek API → deepseek-v4-flash, JSON-only
   Parse + fail-safe → on parse error, return a safe object flagged priority=true
   Upsert Client → hvl_clients (on_conflict=email) → extract id
   Upsert Case   → hvl_cases (on_conflict=client_id,case_type) → extract id
   Insert Review → hvl_document_reviews (rich + legacy columns)
        → Build Paralegal Email → Resend (score ring, present/missing lists, priority banner)
        → Build Client Email    → Resend (confirmation + "documents still needed")
   ▼
Paralegal dashboard (Railway) ── reads ──► Supabase
```

**The data layer (Supabase)**
A relational chain models the hierarchy: a client has cases; a case has reviews.

| Table | Purpose |
|---|---|
| `hvl_clients` | Identity (unique email), contact, country of origin, status |
| `hvl_cases` | client_id FK, case_type, status; unique on (client_id, case_type) so re-submissions update rather than duplicate |
| `hvl_document_reviews` | case_id/client_id FKs, completeness_score (int), completeness_label, documents_present/missing/optional (text[]), ai_summary, ai_notes, confidence_level, case_type, priority_flag, + legacy mirror columns |
| `hvl_attorneys`, `hvl_case_notes` | supporting tables |

**The interfaces**
- Marketing site (Tally intake form): [github](https://github.com/jmcc089/production-ai-automation/tree/main/holt_vargas/web) · [live](https://holtvargas-web.up.railway.app/)
- Paralegal dashboard (prioritized case queue, present/missing lists, priority flag, completeness stats): [github](https://github.com/jmcc089/production-ai-automation/tree/main/holt_vargas/app) · [live](https://holtvargas-app.up.railway.app/)

**QA & verification**
- QA across all four case types with both complete and incomplete packets, including a Spanish-language document set; verified the FK chain populates and priority flagging fires.
- Paralegal report and client confirmation render correctly across major email clients.
- Dashboard reflects live Supabase data; rich review columns populate (not just legacy fields).

---

### 2.3 Brasa Commerce

**The stack**

| Layer | Tool | Purpose |
|---|---|---|
| Intake form | Tally | Customer message → webhook |
| Orchestration | n8n (Railway) | Retrieve → draft → escalation gate → persist → route |
| Retrieval | Supabase FTS | `search_products` RPC over the catalog (RAG) |
| AI | DeepSeek `deepseek-v4-flash` | JSON-only intent + grounded draft |
| Database | Supabase | PostgreSQL + RLS + Storage — catalog + tickets |
| Email | Resend | Customer reply + team notification |
| Frontend | static HTML/JS (Railway) | Marketing site + agent console |

**The automation (n8n)**
Workflow **"Brasa Commerce - Approve & Send"** — two webhook triggers (`brasa-intake` + `brasa-approve`) inside a **single** workflow. The approval fires its own webhook rather than pausing the intake run (ADR-3).

```
Tally form
   │  POST /webhook/brasa-intake
   ▼
n8n — Intake path (Railway)
   Set Fields → name, email, message
   Retrieve Products → Supabase RPC search_products (FTS over bc_product_catalog) → top matches
   Build prompt → inject RETRIEVED PRODUCT CONTEXT; instruct grounding
   DeepSeek API → deepseek-v4-flash, JSON-only (intent, draft_reply, confidence, escalate_flag)
   Parse + rules → force escalate on complaint / confidence < 0.70 / risk keywords
   Upsert Customer → bc_customers → Insert Ticket (status 'sent' if confident, 'draft' if escalated)
   Escalation Gate (IF escalate_flag)
        ├─ false (confident) → Customer Email → Send → Team Email → Send
        └─ true  (escalated) → Team Email → Send         (no customer email yet)
   ▼
Agent dashboard (Railway) ── reads/writes ──► Supabase
   On Approve → POST /webhook/brasa-approve
   ▼
n8n — Approve & Send path
   Build approved email (agent's edited reply) → Send → set ticket status 'sent'
```

**The data layer (Supabase)**

| Table | Purpose |
|---|---|
| `bc_product_catalog` | sku, product_name, category, description, compatibility_notes, price, FAQ (jsonb); the retrieval corpus, searched via `search_products` FTS |
| `bc_customers` | identity (unique email), contact, total_tickets |
| `bc_support_tickets` | customer_id FK, raw_message, intent, draft_reply, final_reply, confidence, escalate_flag, context_used (real retrieved SKUs), status (draft/approved/sent/escalated) |
| `bc_ticket_notes`, `bc_agents` | supporting tables |

**`search_products` function (Postgres full-text search):**

```sql
with q as (
    select to_tsquery(
      'english',
      array_to_string(
        tsvector_to_array(to_tsvector('english', coalesce(query_text, ''))),
        ' | '            -- OR the lexemes so long messages still match
      )
    ) as query
  ),
  ranked as (
    select
      p.sku, p.product_name, p.category, p.description,
      p.compatibility_notes, p.price,
      ts_rank(
        to_tsvector('english',
          coalesce(p.product_name,'')        || ' ' ||
          coalesce(p.description,'')          || ' ' ||
          coalesce(p.compatibility_notes,'')  || ' ' ||
          coalesce(p.category,'')),
        q.query
      ) as rank
    from bc_product_catalog p, q
    where p.active = true
      and q.query is not null
      and to_tsvector('english',
            coalesce(p.product_name,'')       || ' ' ||
            coalesce(p.description,'')         || ' ' ||
            coalesce(p.compatibility_notes,'') || ' ' ||
            coalesce(p.category,'')) @@ q.query
    order by rank desc
    limit 3
  )
  select coalesce(jsonb_agg(to_jsonb(ranked) - 'rank'), '[]'::jsonb) as products
  from ranked;
```

**The interfaces**
- Marketing site (Tally intake form): [github](https://github.com/jmcc089/production-ai-automation/tree/main/brasa_commerce/web) · [live](https://brasacommerce-web.up.railway.app/)
- Agent dashboard (confident tickets auto-sent; held tickets — complaints, low-confidence, risk-keyword — carry an editable draft to approve-and-send or escalate): [github](https://github.com/jmcc089/production-ai-automation/tree/main/brasa_commerce/app) · [live](https://brasacommerce-app.up.railway.app/)

**QA & verification**
- End-to-end test across all five intents, verifying both paths: confident → auto-sent (`status=sent`); escalated → held (`status=draft`), team-notified, then agent approve-and-send flips to `sent`.
- Retrieval verified: `context_used` shows real SKUs; replies cite accurate catalog facts.
- Customer and team emails render across major clients; dashboard approve/escalate actions work against live Supabase.

---

*Full case study, interactive brief, and screenshots: [Notion](https://app.notion.com/p/37cf9cdba745812daa3ffa8a6f81177c)*
