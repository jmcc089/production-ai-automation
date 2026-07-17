# n8n

Build source for the `n8n` Railway service — the automation backbone all three workflows run on (`brasa-intake`/`brasa-approve`, `hvl-intake`, `cedar-intake`).

Not a frontend, not one of the three builds — this folder exists only so the service doesn't depend on a third-party GitHub repo. The Dockerfile just pulls the official `n8nio/n8n` image and sets a few defaults; there is no application code here.

Workflows, credentials, and execution history live in the attached Postgres and `n8n-volume`, not in this repo. `N8N_ENCRYPTION_KEY` is a Railway service variable (Variables tab), independent of this source — that's what keeps credentials decryptable across a source change.

Version is pinned in the Dockerfile (`FROM n8nio/n8n:2.19.2`). Bumping it is a deliberate, separate decision — not something that happens by touching this repo.
