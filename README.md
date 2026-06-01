# SweGen PGx Portal

Public, AI-agent-guided pharmacogenomic analysis of the **SweGen
reference dataset** through Safe Colab. Production realization of
**Figure 3** of the [Safe Colab preprint][paper].

> Live: <https://swegen-pgx-portal.hypha.aicell.io>

[paper]: https://github.com/aicell-lab/safe-colab-cli

## What it does

Anyone with a [Hypha][hypha] account can request access. Once an admin
approves, the user creates an analysis session in the dashboard and
gets a **per-session URL** they paste into an AI agent (Claude, Cursor,
or their own). The agent runs Python inside a Jupyter kernel hosted in
this pod; the **Guardian Agent** reviews every code submission and
every output against the dataset's [sensitivity contract][contract]; a
full audit trail (queries · Guardian decisions · outputs · per-user
session metadata) is mirrored to a Hypha artifact and visible to portal
admins.

[hypha]: https://hypha.aicell.io
[contract]: data/README.md

The portal also exposes a **public Community tab** where approved users
can publish HTML reports of their analyses for everyone to read. Reports
go through admin review before they appear publicly; each one is
rendered in a sandboxed iframe so embedded JavaScript can't reach the
portal's auth state.

## The dataset

| | |
|---|---|
| Source | [Ameur et al., *Eur. J. Hum. Genet.* 25, 1261–1265 (2017)](https://doi.org/10.1038/ejhg.2017.130) |
| What | SweGen pharmacogenomic pilot — pharmacogenomically relevant loci, AF ≥ 1%, GRCh38 |
| Rows | **20,179 variants** |
| Chromosomes | 12 (`chr1, chr2, chr4, chr6, chr7, chr8, chr10, chr12, chr13, chr16, chr18, chr19`) |
| Schema | `chrom, pos, id (rsID), ref, alt, af` (tab-separated) |
| Aggregation | **AF-only, sites-only** — no individual genotypes, no PII |
| Data owner | Adam Ameur, Uppsala University / SciLifeLab |

The full sensitivity contract — what's allowed (aggregate AF analyses,
Ti/Tv ratios, substitution spectrum, per-chromosome density,
pharmacogene grouping) and what's blocked (exfiltration, host probing,
secret extraction, raw-row dumps) — is in [`data/README.md`](data/README.md).
That file is read verbatim by the Guardian Agent at startup.

## Suggested analyses

The Safe Colab paper (Figure 3) demonstrates four headline analyses on
this exact dataset:

1. Allele-frequency spectrum stratified by variant type (SNV / insertion / deletion)
2. Ti/Tv ratio per chromosome (expected ≈ 2.0)
3. Six-class substitution spectrum (C>A, C>G, C>T, T>A, T>C, T>G)
4. Per-chromosome AF density

Any aggregate analysis that fits the sensitivity contract is welcome —
join the variants against ClinVar / PharmGKB / CPIC star alleles you
load from inside your session, etc.

## Architecture (one-paragraph version)

A single FastAPI pod (`portal/app.py`) runs the user-facing web UI, an
RPC layer for AI agents, a per-session Jupyter kernel pool with
idle/lifetime reapers, and an HTTP client to a remote **Guardian
Agent** (deployed separately). User identity comes from a Hypha JWT;
admin status from an `ADMIN_EMAILS` allow-list. All state — users,
sessions, audit events — lives in a single **Hypha artifact**
(`swegen-pgx-portal-state`). Every Guardian decision is independently
POSTed by the Guardian to the portal's `/audit/callback` endpoint, so
the audit trail has a defence-in-depth second source.

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the long
version with a diagram.

## Local development

```bash
git clone https://github.com/aicell-lab/swegen-pgx-portal.git
cd swegen-pgx-portal

python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# point at the live Guardian (or `docker run` your own from safe-colab-cli)
export HYPHA_SERVER_URL=https://hypha.aicell.io
export HYPHA_TOKEN=...                # your Hypha login token
export GUARDIAN_URL=http://safe-colab-guardian.hypha.aicell.io
export GUARDIAN_TOKEN=$HYPHA_TOKEN
export RESEND_API_KEY=...              # optional, for email
export AUDIT_HMAC_TOKEN=$(openssl rand -hex 32)
export PORTAL_BASE_URL=http://localhost:8080
export PORTAL_DATA_DIR=$(pwd)/data
export PORTAL_WORK_DIR=$(pwd)/work
mkdir -p work

uvicorn portal.app:app --host 0.0.0.0 --port 8080 --reload
```

Now open <http://localhost:8080>.

## Deploy

The single-replica K8s deployment lives in
[`deploy/k8s-kth.yaml`](deploy/k8s-kth.yaml). The full procedure is in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Short version:

```bash
# Build (must be linux/amd64; KTH cluster is x86):
docker buildx build --platform linux/amd64 \
    -f portal/Dockerfile -t <user>/swegen-pgx-portal:<tag> --push .

# Create / update the secret (admin tokens + Resend key + audit HMAC):
source .env && kubectl --context scilifelab-2-dev -n hypha create secret generic \
  swegen-pgx-portal-secrets \
  --from-literal=HYPHA_TOKEN="$HYPHA_TOKEN" \
  --from-literal=GUARDIAN_TOKEN="$HYPHA_TOKEN" \
  --from-literal=RESEND_API_KEY="$RESEND_API_KEY" \
  --from-literal=AUDIT_HMAC_TOKEN="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl --context scilifelab-2-dev apply -f -

# Apply the manifest + roll out:
kubectl --context scilifelab-2-dev apply -f deploy/k8s-kth.yaml
kubectl --context scilifelab-2-dev -n hypha set image \
    deployment/swegen-pgx-portal portal=<user>/swegen-pgx-portal:<tag>
```

## End-to-end smoke test

After deploy, [`tests/smoke_test.py`](tests/smoke_test.py) walks the
full flow against a live URL (signup → admin approve → create session
→ guardian-checked run → block-on-exfiltration → audit trail).

```bash
PORTAL_URL=https://swegen-pgx-portal.hypha.aicell.io \
HYPHA_TOKEN=... \
python3 tests/smoke_test.py
```

## Credits

- **Data:** Adam Ameur and the SweGen / Uppsala team.
- **Framework:** Safe Colab (Hugo Dettner Källander, Joanna Hård,
  Simin Zhang, Nils Mechtel, Adam Ameur, Wei Ouyang). KTH Royal
  Institute of Technology / SciLifeLab.
- **Hosting:** KTH `scilifelab-2-dev` Kubernetes cluster.
- **Infra:** Hypha (login, artifact manager, RPC).

## License

Apache-2.0 — see [LICENSE](LICENSE).
The **dataset** itself is © Ameur et al. 2017 and is used here under
its original publication conditions.
