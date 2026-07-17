# Production AI Automation — Three builds, one method

Frontends and automation backbone for three AI intake-automation builds, each in a different industry, all sharing one method and one platform.

The same automation pattern — unstructured message in, structured triage out — applied where "wrong" means something different each time: a clinic, an immigration law firm, and a cookware retailer. The design decisions diverge even though the pattern holds. The clearest tell is grounding: Cedar classifies with no retrieval, Holt & Vargas grounds on in-prompt checklists, Brasa uses true retrieval over a Postgres FTS catalog.

Full system design, ADRs, and build evidence: **[Notion case study](https://app.notion.com/p/37cf9cdba745812daa3ffa8a6f81177c)**

> Demo businesses. The systems are real, deployed, and live; the companies are simulated to frame the problem.

## Layout

Each leaf is a self-contained deployable unit — its own `index.html`, `package.json`, and `.nixpacks.toml`. One Railway service per folder.

| Folder | Railway service | Live URL |
|---|---|---|
| `cedar_healthcare/web` | cedarhealthcare-web | https://cedarhealthcare-web.up.railway.app |
| `cedar_healthcare/app` | cedarhealthcare-app | https://cedarhealthcare-app.up.railway.app |
| `holt_vargas/web` | holtvargas-web | https://holtvargas-web.up.railway.app |
| `holt_vargas/app` | holtvargas-app | https://holtvargas-app.up.railway.app |
| `brasa_commerce/web` | brasacommerce-web | https://brasacommerce-web.up.railway.app |
| `brasa_commerce/app` | brasacommerce-app | https://brasacommerce-app.up.railway.app |
| `n8n` | n8n | https://n8n-production-3503.up.railway.app |

`web` = public marketing site with the embedded Tally intake form. `app` = the internal operator dashboard. `n8n` = the shared automation backbone all three builds run on — see [n8n/README.md](n8n/README.md).

## Architecture

The frontends are static sites; the automation lives in n8n; the data lives in Supabase. `n8n/` in this repo is only the container build source (a Dockerfile pulling the official image) — workflows, credentials, and execution history live in the attached Postgres and volume, not in git.

```
Tally form (embedded in web/)
   │  POST /webhook/{cedar-intake | hvl-intake | brasa-intake}
   ▼
n8n (Railway) ── classify / score / retrieve → validate → persist → route
   │                                    │
   ├── DeepSeek deepseek-v4-flash       └── Resend (email)
   ▼
Supabase (Postgres)  ◄── reads/writes ── app/ dashboard
```

All three builds share **one** Supabase project and **one** n8n instance, isolated logically by table prefix — `ch_` (Cedar), `hvl_` (Holt & Vargas), `bc_` (Brasa). That is a deliberate trade-off, documented as Cedar ADR-5: it minimizes operational overhead across a multi-client portfolio at the cost of logical rather than physical isolation. This monorepo is the frontend expression of that same decision.

## Deployment

Railway builds each service with **Root Directory** set to its folder, so Nixpacks resolves `package.json` and `.nixpacks.toml` within that folder. **Watch Paths** are scoped per folder so a change to one build doesn't rebuild the other five.

The three `app` dashboards receive Supabase credentials at build time. `npm run build` substitutes `__RAILWAY_URL__` and `__RAILWAY_KEY__` placeholders in `index.html` with the service's environment variables:

| Variable | Set on | Value |
|---|---|---|
| `SUPABASE_URL` | the three `app` services | project URL |
| `SUPABASE_KEY` | the three `app` services | **anon** key — never `service_role` |

The `web` services need no environment variables.

The key shipped to the browser is the Supabase `anon` key, which is public by design; row-level security is what protects the data behind it. Scoping RLS across the patient, client, and ticket tables is tracked as outstanding hardening work in the Notion case study.

## Local development

```bash
cd cedar_healthcare/web && npx serve . -p 3000
```

The `app` dashboards read their credentials from placeholders substituted at build time, so a local `npx serve` renders the shell without live data. To run one against real data, substitute the values first — but do not commit the result:

```bash
cd cedar_healthcare/app
SUPABASE_URL="..." SUPABASE_KEY="..." npm run build   # edits index.html in place
npx serve . -p 3000
git checkout index.html                               # restore placeholders
```

Note `npm run build` uses GNU `sed -i` syntax, matching the Linux build container. On macOS it needs `sed -i ''`.
