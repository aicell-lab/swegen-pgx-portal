# Operator brief — SweGen PGx Portal

You are an AI agent assigned to this repository. This file gives you
everything you need to operate the project without relying on any
sibling repo or session context.

If you only read one section, read **Operating posture** at the bottom.

## What this project is

A **public, AI-agent-guided pharmacogenomic analysis portal** built on
top of the Safe Colab framework. Users sign in with Hypha, request
access, and once approved get a unique URL they paste into an AI agent
(Claude, Cursor, etc.). The agent runs Python code in a per-session
IPython kernel hosted in this pod; a remote Guardian Agent reviews every
submission and every result against the dataset's sensitivity contract;
all activity is recorded in a Hypha artifact and surfaced to admins.

The dataset is the **SweGen pharmacogenomic pilot** — 20,179 variants ×
population allele frequencies extracted from the 1,000-Swede SweGen
reference cohort (Ameur et al., *Eur. J. Hum. Genet.* 25, 1261–1265,
2017), restricted to pharmacogenomically relevant loci with AF ≥ 1%.
**Aggregate-only, no individual genotypes.**

This is the production realization of **Figure 3 of the Safe Colab
preprint**.

## Stakeholders

- **Adam Ameur** (`adam.ameur@igp.uu.se`) — data owner, Uppsala
  University. Approves access requests.
- **Joanna Hård** (`joanna.hard@scilifelab.se`) — SciLifeLab co-admin.
- **Wei Ouyang** (`wei.ouyang@scilifelab.se`) — KTH, technical lead.
  Currently the **Resend account owner** — see "Email" below.

## Live deployment

| Thing | Where |
|---|---|
| Public URL | https://swegen-pgx-portal.hypha.aicell.io |
| Guardian (external) | http://safe-colab-guardian.hypha.aicell.io |
| Guardian (in-cluster) | http://safe-colab-guardian.hypha.svc.cluster.local |
| Cluster | KTH `scilifelab-2-dev` (kubeconfig context) |
| Namespace | `hypha` |
| Deployment name | `swegen-pgx-portal` |
| Service name | `swegen-pgx-portal` (ClusterIP, port 80→8080) |
| Ingress | `swegen-pgx-portal` — TLS via cert-manager + letsencrypt-prod |
| Image registry | Docker Hub — `oeway/swegen-pgx-portal:<tag>` |
| State artifact | Hypha alias `safe-colab/swegen-pgx-portal-state` |

The Guardian deployment is **NOT** in this repo — it lives in the
`safe-colab-cli` repo (`security-agent-app/`). The patch this portal
depends on (an optional `audit_callback` field on the security-check
endpoints) was shipped in `oeway/safe-colab-guardian:0.18.0`. If you
need to modify Guardian behaviour, do it there, not here.

## Operating posture

**Default to caution. This is a public-facing service that has been
announced (or is about to be) and is used to demonstrate zero-trust
data serving. A bad day here costs trust.**

- The portal pod runs as a **single replica**. Scaling out would split
  in-memory kernels across pods and break the URL-as-secret model. Do
  not change `replicas`.
- A rollout briefly takes the service down (≤30s) — fine for low
  traffic, schedule outside press-event windows.
- Audit data is the value proposition. Do not change the artifact
  schema (`users/`, `sessions/`, `audit/{session_id}/*.json`) without a
  migration plan.
- The dataset is owned by Adam Ameur. **Do not change `data/README.md`**
  (the sensitivity contract) without his approval — the Guardian reads
  it verbatim as the policy it enforces.
- Don't expose anything that returns row-level VCF data. Guardian is a
  defence-in-depth layer, not a substitute for sane endpoints.

## Repo layout

```
swegen-pgx-portal/
├── data/                       baked into the image at /data
│   ├── README.md               dataset description + sensitivity contract
│   └── swegen_pgx_pilot.sites.pass_AF_0.01_filtered.vcf
├── portal/
│   ├── app.py                  FastAPI app (all endpoints)
│   ├── auth.py                 Hypha JWT validation + admin allow-list
│   ├── store.py                Hypha-artifact-backed user/session/audit store
│   ├── kernel.py               minimal IPython kernel wrapper
│   ├── kernel_pool.py          per-session kernel pool with reaper
│   ├── guardian_client.py      HTTP client; attaches audit_callback
│   ├── email_resend.py         Resend API integration
│   ├── static/                 vanilla-JS frontend (index / dashboard / session / admin)
│   ├── requirements.txt
│   └── Dockerfile
├── deploy/
│   └── k8s-kth.yaml            scilifelab-2-dev deployment manifest
├── docs/
│   ├── ARCHITECTURE.md         how the pieces fit together
│   └── DEPLOYMENT.md           step-by-step deploy guide
├── tests/
│   └── smoke_test.py           end-to-end check against a live URL
├── pyproject.toml
├── LICENSE
├── README.md
└── CLAUDE.md   (this file)
```

## Common tasks

### Make a code change and ship it

```bash
# 1. edit something under portal/
# 2. build + push (must be linux/amd64; cluster is x86)
docker buildx build --platform linux/amd64 \
    -f portal/Dockerfile -t oeway/swegen-pgx-portal:<new-tag> --push .

# 3. roll out
kubectl --context scilifelab-2-dev -n hypha set image \
    deployment/swegen-pgx-portal portal=oeway/swegen-pgx-portal:<new-tag>
kubectl --context scilifelab-2-dev -n hypha rollout status \
    deployment/swegen-pgx-portal --timeout=120s

# 4. verify
curl -sS https://swegen-pgx-portal.hypha.aicell.io/api/healthz
```

Use a bumped tag (`0.2.1`, `0.2.2`, …) each time. Don't reuse tags.

### Check logs

```bash
kubectl --context scilifelab-2-dev -n hypha logs \
    deployment/swegen-pgx-portal --tail=200
```

Things you'll see:
- `Portal ready. base_url=…` — startup
- `Store hydrated: N users, N sessions, N audit events` — artifact OK
- `Resend ok: sent to [...]` — email delivered (test-mode caveat: see below)
- `Started kernel for session …`, `Reaped session …` — kernel pool

### Rotate the Resend API key

The key lives in the `swegen-pgx-portal-secrets` secret. Recreate with:

```bash
source .env && kubectl --context scilifelab-2-dev -n hypha create secret generic \
  swegen-pgx-portal-secrets \
  --from-literal=HYPHA_TOKEN="$HYPHA_TOKEN" \
  --from-literal=GUARDIAN_TOKEN="$HYPHA_TOKEN" \
  --from-literal=RESEND_API_KEY="<new-key>" \
  --from-literal=AUDIT_HMAC_TOKEN="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl --context scilifelab-2-dev apply -f -

kubectl --context scilifelab-2-dev -n hypha rollout restart deployment/swegen-pgx-portal
```

### Inspect the state artifact directly

```bash
source .env && hypha art ls safe-colab/swegen-pgx-portal-state
# or in Python (more flexible):
python3 -c "
import asyncio, os, httpx
from hypha_rpc import connect_to_server
async def go():
    s = await connect_to_server({'server_url':'https://hypha.aicell.io','token':os.environ['HYPHA_TOKEN']})
    am = await s.get_service('public/artifact-manager')
    for d in ['users','sessions']:
        for f in await am.list_files(artifact_id='safe-colab/swegen-pgx-portal-state', dir_path=d):
            print(d, f.get('name'))
asyncio.run(go())
"
```

## Email — current state and the gotcha

Resend (`re_cDB85YvP_…`) is in **test mode**: the only verified sender
is `onboarding@resend.dev`, and Resend will only accept recipients
matching the account-owner email (`wei.ouyang@scilifelab.se`).

For now, the portal ships all admin notifications to Wei with the
intended-recipient list rendered in the email body. Wei forwards.

**Permanent fix when ready:**

1. Add a domain at <https://resend.com/domains> (e.g.
   `notifications.hypha.aicell.io`) — needs the DNS records Resend
   provides.
2. Update the deployment env:
   - `EMAIL_FROM=SweGen PGx Portal <portal@notifications.hypha.aicell.io>`
   - Remove `RESEND_VERIFIED_TO` (or leave empty)
3. Roll out.

After that every notification goes straight to all three admins.

## Auth model

- **Users** authenticate with Hypha (`hypha.aicell.io`) — we trust the
  Hypha JWT. The `email` claim becomes the canonical user ID.
- **Admins** are the three emails listed in `ADMIN_EMAILS` on the
  deployment. There is no role-claim parsing; we just match emails.
- **Agents** authenticate via the **URL-as-secret** session token in
  the path: `/s/<token>/SKILL.md`, `/s/<token>/api/run_code`. Anyone
  with the URL can run code in that session — treat it like a password.
- **Guardian → portal audit** is HMAC-checked via the
  `X-Audit-Token` header against `AUDIT_HMAC_TOKEN` (random per
  deployment).

## Community reports

Approved users can publish HTML reports to the public **Community**
tab through their AI agent. Flow:

1. Agent in a session calls `POST /s/<token>/api/publish_report` with
   `{title, description, html, tags}`. HTML cap is `5 MB`.
2. The portal stores the report in the state artifact under
   `reports/<report_id>/` (`manifest.json` + `report.html`) with
   `status=pending`.
3. Admins get a Resend email with a Preview button (`/admin/reports/
   <id>/preview` — renders the report inside an iframe with a strict
   CSP and `sandbox="allow-scripts"` only).
4. An admin approves or rejects from `/admin#reports`. On approve, the
   author gets an email; the report becomes visible at `/community`.
5. On reject, the author gets an email with the reviewer's note; the
   report stays in the artifact in `status=rejected` (deletable from
   the admin panel).

**Safety stance:**
- Reports may contain attacker-controlled HTML and JavaScript.
- They are rendered exclusively inside `<iframe sandbox="allow-scripts">`
  *without* `allow-same-origin`, so the iframe gets a null origin and
  cannot read this site's cookies or localStorage.
- A strict CSP on the raw-HTML response (`default-src 'none'; img-src data:;
  script-src 'unsafe-inline'; style-src 'unsafe-inline' data:;
  font-src data:;`) blocks cross-origin network calls and external
  resource loads.
- The admin is the final gate before public visibility — they preview
  in the same sandbox the public sees.

**API surface (admin):**
- `GET  /api/admin/reports` — list all reports
- `POST /api/admin/reports/approve` `{report_id, note}`
- `POST /api/admin/reports/reject` `{report_id, note}`
- `DELETE /api/admin/reports/{report_id}` — hard delete (rare cleanup)

**API surface (public):**
- `GET  /api/community/reports` — list of approved reports
  (anonymous; user emails are stripped from this listing)
- `GET  /api/community/reports/{report_id}` — single report metadata
- `GET  /community/reports/{report_id}/raw.html` — the HTML body, only
  if approved; served with the strict CSP

**API surface (agent, per session):**
- `POST /s/<token>/api/publish_report`
- `GET  /s/<token>/api/my_reports` — author's own reports + status

## Session lifecycle

The kernel pool reaps sessions automatically.

- **Idle timeout** — `PORTAL_KERNEL_IDLE_SEC` (default **30 min**). No
  activity for that long → kernel stopped, session marked `ended`,
  `session_auto_ended` audit event appended (reason `idle_timeout`).
- **Hard lifetime** — `PORTAL_KERNEL_MAX_LIFETIME_SEC` (default **24h**).
  Even active sessions get reaped at the cap (reason `max_lifetime`).
- **Pool capacity** — `PORTAL_MAX_KERNELS` (default 32). New session
  needing a slot evicts the longest-idle one (reason `pool_capacity`).
- **User-initiated end** — DELETE `/api/sessions/{id}` from the
  dashboard (reason `user_request`).
- **Reaper cadence** — `PORTAL_REAPER_INTERVAL_SEC` (default 5 min).

Once a session is `ended`, calls to its `/s/<token>/...` URL return
HTTP 410. The token cannot be revived; the user creates a new session.

## Where to ask for help

- Hypha SDK / artifact manager / login flow:
  <https://hypha.aicell.io/ws/agent-skills/SKILL.md> and the GUIDE
  directory linked from there.
- Cluster / cert-manager / nginx-ingress: look at other deployments
  in the same namespace (`svamp-webapp`, `safe-colab-guardian`,
  `agentic-data-clean-room`) — they show the working pattern.
- Guardian Agent internals: `safe-colab-cli/security-agent-app/`.

## Things you should not do

- Do **not** scale the deployment to >1 replica.
- Do **not** add an endpoint that returns rows from the VCF directly
  (`pos`, `id`, `af` lookups are fine in the form of analyses, but
  there is no business case for a "raw row dump" path).
- Do **not** commit secrets. The `swegen-pgx-portal-secrets` k8s
  secret is the only place they should live.
- Do **not** lower the Guardian's authority — the Guardian's decision
  is final on whether a code submission runs.
- Do **not** trust `metadata.session_id` in the `/audit/callback`
  payload without verifying the HMAC token; that's the integrity
  boundary for the Guardian-side audit trail.
