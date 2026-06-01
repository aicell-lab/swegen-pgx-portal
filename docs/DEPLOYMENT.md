# Deployment

The portal is deployed on the KTH `scilifelab-2-dev` Kubernetes
cluster in the `hypha` namespace, behind cert-manager / letsencrypt.

## Prerequisites

You need:

- `kubectl` with the `scilifelab-2-dev` context configured.
- Docker (or any buildkit-capable builder) — must target `linux/amd64`.
- A `.env` file (at the repo root or sourced from elsewhere) with:
  ```
  HYPHA_TOKEN=<long-lived token for the safe-colab workspace>
  RESEND_API_KEY=<resend key>
  ```
- The Guardian Agent deployed in the same cluster
  (`safe-colab-guardian` deployment in `hypha` namespace).
  This is **not** in this repo — see `safe-colab-cli/security-agent-app/`.
  The portal requires the version of the Guardian image that supports
  the `audit_callback` field on `/ensure_code_secure` and
  `/ensure_output_secure` (≥ `oeway/safe-colab-guardian:0.18.0`).

## First deploy

### 1. Create the secret

```bash
source .env && kubectl --context scilifelab-2-dev -n hypha create secret generic \
  swegen-pgx-portal-secrets \
  --from-literal=HYPHA_TOKEN="$HYPHA_TOKEN" \
  --from-literal=GUARDIAN_TOKEN="$HYPHA_TOKEN" \
  --from-literal=RESEND_API_KEY="$RESEND_API_KEY" \
  --from-literal=AUDIT_HMAC_TOKEN="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl --context scilifelab-2-dev apply -f -
```

### 2. Build and push the image

```bash
docker buildx build --platform linux/amd64 \
    -f portal/Dockerfile \
    -t <user>/swegen-pgx-portal:<tag> \
    --push .
```

### 3. Apply the manifest

`deploy/k8s-kth.yaml` already pins the image to a specific tag — if
that tag isn't yours, set it explicitly after applying:

```bash
kubectl --context scilifelab-2-dev apply -f deploy/k8s-kth.yaml
kubectl --context scilifelab-2-dev -n hypha set image \
    deployment/swegen-pgx-portal portal=<user>/swegen-pgx-portal:<tag>
kubectl --context scilifelab-2-dev -n hypha rollout status \
    deployment/swegen-pgx-portal --timeout=180s
```

### 4. Verify

```bash
curl -sS https://swegen-pgx-portal.hypha.aicell.io/api/healthz
curl -sS https://swegen-pgx-portal.hypha.aicell.io/api/config | jq .admin_emails
```

The first request also forces TLS cert provisioning if it hasn't
happened yet — cert-manager will issue a Let's Encrypt cert via
http-01. Check progress with:

```bash
kubectl --context scilifelab-2-dev -n hypha get certificate \
    swegen-pgx-portal.hypha.aicell.io-tls
```

### 5. Optional — run the smoke test

```bash
PORTAL_URL=https://swegen-pgx-portal.hypha.aicell.io \
HYPHA_TOKEN=$HYPHA_TOKEN \
python3 tests/smoke_test.py
```

## Subsequent rollouts

```bash
# bump version
docker buildx build --platform linux/amd64 \
    -f portal/Dockerfile -t <user>/swegen-pgx-portal:<new-tag> --push .

kubectl --context scilifelab-2-dev -n hypha set image \
    deployment/swegen-pgx-portal portal=<user>/swegen-pgx-portal:<new-tag>
kubectl --context scilifelab-2-dev -n hypha rollout status \
    deployment/swegen-pgx-portal --timeout=120s
```

A rollout briefly takes the portal offline (a few seconds). The
deployment uses `strategy: Recreate` because we want at most one
replica alive at any time (single-replica architecture, see
ARCHITECTURE.md).

## Configuration reference

All env vars used by the portal:

| Env | Default | Purpose |
|---|---|---|
| `HYPHA_SERVER_URL` | `https://hypha.aicell.io` | Hypha server we talk to |
| `HYPHA_WORKSPACE` | `safe-colab` | Workspace owning the state artifact |
| `HYPHA_TOKEN` | (secret) | Service account token for the workspace |
| `GUARDIAN_URL` | `http://safe-colab-guardian.hypha.svc.cluster.local` | Guardian endpoint |
| `GUARDIAN_TOKEN` | (secret, falls back to `HYPHA_TOKEN`) | Bearer for the Guardian |
| `RESEND_API_KEY` | (secret) | Resend API key for admin notifications |
| `RESEND_VERIFIED_TO` | `wei.ouyang@scilifelab.se` | Until a Resend domain is verified, all admin emails are routed here with the intended-recipient list rendered in the body. Unset once a domain is verified. |
| `EMAIL_FROM` | `SweGen PGx Portal <onboarding@resend.dev>` | Sender; switch to a verified-domain sender when ready |
| `PORTAL_BASE_URL` | `http://localhost:8080` | Public URL the portal advertises to itself (for SKILL.md, emails, agent URLs) |
| `AUDIT_CALLBACK_URL` | derived | URL the Guardian POSTs audit events to — usually the in-cluster Service DNS to avoid TLS / ingress hop |
| `AUDIT_HMAC_TOKEN` | (secret) | Header secret on the audit callback path |
| `PORTAL_DATA_DIR` | `/data` | Mounted dataset (read-only at runtime) |
| `PORTAL_WORK_DIR` | `/work` | Kernel working directory |
| `ARTIFACT_ALIAS` | `swegen-pgx-portal-state` | Hypha artifact alias for state |
| `ADMIN_EMAILS` | (three project admins) | Comma-separated allow-list for `/api/admin/*` |
| `PORTAL_MAX_KERNELS` | `32` | Hard cap on concurrent kernels |
| `PORTAL_KERNEL_IDLE_SEC` | `1800` (30 min) | Per-session idle timeout |
| `PORTAL_KERNEL_MAX_LIFETIME_SEC` | `86400` (24 h) | Hard cap on session lifetime |
| `PORTAL_REAPER_INTERVAL_SEC` | `300` (5 min) | Reaper cadence |

## Tuning the lifecycle for a quiet vs busy portal

If the portal is mostly idle most of the time (typical for a research
portal), keep defaults — the reaper cleans up dead sessions every 5
minutes, well before they accumulate.

If you expect a press-event rush, consider:

- Drop `PORTAL_KERNEL_IDLE_SEC` to `600` (10 min) so curious-but-not-
  serious visitors free their slot faster.
- Bump `PORTAL_MAX_KERNELS` to `64` and bump the pod's memory limit.
- Lower `PORTAL_REAPER_INTERVAL_SEC` to `60` for faster turnover.

## Disaster recovery

Because all state lives in the `swegen-pgx-portal-state` Hypha
artifact, you can rebuild the cluster from scratch and the user list,
session list, and full audit history come back automatically when the
new pod hydrates.

To **wipe and start fresh** (for testing):

```python
# wipe everything in the artifact
import asyncio, os
from hypha_rpc import connect_to_server

async def go():
    s = await connect_to_server({"server_url": "https://hypha.aicell.io",
                                 "token": os.environ["HYPHA_TOKEN"]})
    am = await s.get_service("public/artifact-manager")
    aid = "safe-colab/swegen-pgx-portal-state"
    try: await am.edit(artifact_id=aid, stage=True)
    except Exception: pass
    for d in ["users", "sessions"]:
        for f in await am.list_files(artifact_id=aid, dir_path=d):
            await am.remove_file(artifact_id=aid, file_path=f"{d}/{f['name']}")
    for sess in await am.list_files(artifact_id=aid, dir_path="audit"):
        if sess.get("type") == "directory":
            for ev in await am.list_files(artifact_id=aid, dir_path=f"audit/{sess['name']}"):
                await am.remove_file(artifact_id=aid, file_path=f"audit/{sess['name']}/{ev['name']}")
    await am.commit(artifact_id=aid)

asyncio.run(go())
```

Then restart the deployment to rehydrate the in-memory cache:
`kubectl rollout restart deployment/swegen-pgx-portal -n hypha`.
