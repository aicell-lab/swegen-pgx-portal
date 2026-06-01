# Architecture

## Overview

Single FastAPI process (`portal.app`) that does five things:

1. **Web UI** — landing, dashboard, session, admin pages (static HTML
   in `portal/static/`).
2. **REST + RPC** — signup / sessions / admin endpoints, plus the
   agent-facing `/s/<token>/...` URL-as-secret endpoints.
3. **Per-session Jupyter kernels** — one `PortalKernel` per portal
   session, all running inside the same pod (weak isolation).
4. **Guardian client** — every `run_code` is double-checked
   (pre-execution + post-execution) against an externally deployed
   Guardian Agent.
5. **Audit + state store** — every user, session, and audit event is
   persisted in a single Hypha artifact.

```
                          ┌────────────────────────────────────────┐
                          │              Hypha login               │
                          └────────────────────┬───────────────────┘
                                               │ JWT
                                               ▼
  Browser ────────────────────▶ ┌──────────────────────────┐
                                │     FastAPI portal pod    │
  AI agent ──/s/<token>/…──────▶│  app · auth · store · pool│
                                │  guardian_client · resend │
                                └─────────┬───────────┬─────┘
                                          │           │
                          per-call ──HTTPS┘           │ persistent
                          guardian check              │ state
                                          │           ▼
                                          │   ┌───────────────────┐
                                          │   │  Hypha artifact   │
                                          │   │  swegen-pgx-      │
                                          │   │  portal-state     │
                                          │   │  · users/         │
                                          │   │  · sessions/      │
                                          │   │  · audit/         │
                                          │   └───────────────────┘
                                          ▼
                                  ┌──────────────────┐
                                  │  Guardian Agent  │
                                  │ (separate K8s    │
                                  │  deployment in   │
                                  │  safe-colab-cli) │
                                  └────────┬─────────┘
                                           │ POST /audit/callback
                                           ▼
                                    back into FastAPI pod
```

## State store (Hypha artifact)

Alias: `safe-colab/swegen-pgx-portal-state`. Layout:

```
users/<base64url(email)>.json   – one file per user
sessions/<uuid>.json            – one file per session
audit/<session_uuid>/<ts>_<id>.json
                                 – one file per audit event
```

Writes are write-through: `edit(stage=True)` → `put_file` (presigned
PUT URL) → `commit`. The portal also maintains an in-memory cache; on
startup it hydrates everything from the artifact.

This is intentionally not a database. Tradeoffs:

- ✔ Zero local state — the pod is restart-safe and easy to relocate.
- ✔ Hypha already handles auth + permissions.
- ✗ Each write incurs an artifact commit (~150ms). Fine at MVP volume.
  If write volume grows, batch audit events or move to a DB.

## Kernel pool

`KernelPool` maps `session_id → PortalKernel`. Lazy: a kernel is
spawned the first time the session's `/s/<token>/api/run_code` is
called. After that, variables persist across calls (standard Jupyter
behaviour).

Reasons a kernel exits:

| Reason | Default | Tunable via |
|---|---|---|
| `user_request` | n/a | DELETE `/api/sessions/{id}` |
| `idle_timeout` | 30 min | `PORTAL_KERNEL_IDLE_SEC` |
| `max_lifetime` | 24 h | `PORTAL_KERNEL_MAX_LIFETIME_SEC` |
| `pool_capacity` | when slot needed | `PORTAL_MAX_KERNELS` (32) |
| `shutdown` | on SIGTERM | (pod terminating) |

The reaper runs every `PORTAL_REAPER_INTERVAL_SEC` (5 min). On reap
the pool calls `on_session_ended(session_id, reason)` which:

1. updates the session record's `status` to `ended`
2. appends a `session_auto_ended` audit event with `{reason}`

After that, any `/s/<token>/...` request to that session returns
HTTP 410. The token cannot be revived — the user creates a new session
from the dashboard.

## Authentication

| Surface | Auth | What it grants |
|---|---|---|
| `/api/me`, `/api/signup`, `/api/sessions*` | Hypha JWT in `Authorization: Bearer` | acting as the JWT's email |
| `/api/admin/*` | Hypha JWT + `email ∈ ADMIN_EMAILS` | admin actions |
| `/s/<token>/SKILL.md`, `/s/<token>/api/run_code` | URL-as-secret (the token itself) | code-execution in that session's kernel |
| `/audit/callback` | `X-Audit-Token` header = `AUDIT_HMAC_TOKEN` | append audit events |

JWT validation does a fast local decode (no network) plus a
best-effort cross-check against Hypha's `parse_token` endpoint
(`auth.py`).

## Guardian audit-callback path

The Guardian endpoint (`security-agent-app/app.py` in `safe-colab-cli`)
accepts an optional `audit_callback` block:

```json
{
  "url": "http://swegen-pgx-portal.hypha.svc.cluster.local/audit/callback",
  "token": "<AUDIT_HMAC_TOKEN>",
  "metadata": {"session_id": "...", "user_email": "..."}
}
```

After each pre/post check the Guardian fires-and-forgets a POST to
`url` with the decision + the original code/output + the metadata. The
portal verifies `X-Audit-Token` and appends an audit event.

This produces two parallel audit streams:

- **Portal-side** events (`code_submitted`, `code_executed`,
  `guardian_pre_blocked`, `guardian_post_blocked`,
  `session_auto_ended`) — written by the portal's run_code handler.
- **Guardian-side** events (`guardian_ensure_code_secure`,
  `guardian_ensure_output_secure`) — written from the Guardian's POST.

Both streams are stored in the same per-session audit folder and
rendered together (chronologically) in the admin UI. The point is
defence-in-depth: if portal logging fails or is tampered, the
Guardian's record remains.

## Frontend

Vanilla JS, no build step. Each page loads `/static/portal.js`
(`PortalAPI` global: `hyphaLogin`, `api(path, opts)`, `getMe`, …) and
its own page-specific inline script.

Hypha login uses the canonical `hypha-rpc` browser pattern:

```js
const token = await rpc.login({
  server_url: cfg.hypha_server_url,
  login_callback: async (ctx) => {
    window.open(ctx.login_url, "hypha-login", "width=520,height=700,…");
  },
});
```

— stored in `localStorage` as `portal_hypha_token`, sent as
`Authorization: Bearer <token>` on every API call.

## Community-report publishing

Per-session agent endpoint `POST /s/<token>/api/publish_report` accepts
`{title, description, html, tags}`. The portal:

1. Validates the session is active and the HTML is ≤ 5 MB.
2. Writes `reports/<report_id>/manifest.json` (metadata) and
   `reports/<report_id>/report.html` (body) into the state artifact.
3. Marks `status=pending`, appends a `report_submitted` audit event to
   the session, and emails admins.

Admin review (`/admin#reports`) uses `/admin/reports/<id>/preview`,
which streams the raw HTML with a strict CSP into a sandboxed iframe.
Approve / reject endpoints flip `status` and email the author. The
public `/community` page lists approved reports (anonymous-friendly,
no user emails) and renders each in `<iframe sandbox="allow-scripts">`
pointing at `/community/reports/<id>/raw.html`.

**Why this is reasonably safe:**

- Report HTML can be hostile, but the iframe has a null origin (no
  `allow-same-origin`), so the embedded JS cannot read this site's
  cookies or `localStorage`.
- The CSP on the report response blocks all network calls except
  inline scripts/styles and `data:` images — so a report can't beacon
  the visitor's IP to a third party.
- Admin review is the publication gate. The same sandbox is used at
  preview time, so what the admin sees is exactly what the public
  will see.

## Why a single replica

The pod holds all in-memory kernels (one per session) and the
URL-as-secret routing table that maps a session token to its kernel.
Sharding sessions across replicas would require either sticky routing
based on the URL secret (hard) or moving kernel state out of process
(heavy). For the expected access pattern of this portal (low dozens of
concurrent sessions) a single replica with a generous resource budget
is the right call.

## Why Hypha artifact instead of a DB

- The portal needs to be **restart-safe** without a local volume.
- All state needs to be **inspectable** by humans and other Hypha
  clients — that's free with an artifact, and writing a side-channel
  DB just to mirror to an artifact is more code.
- Reads from the artifact are cached in memory after hydrate; only
  writes hit the network, and write rate is bounded by guardian
  decision latency (~1–5s).
