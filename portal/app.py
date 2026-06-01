"""SweGen PGx Portal — FastAPI app.

Single long-lived pod hosting:
 - landing page + user dashboard + admin dashboard (static HTML)
 - signup / approval flow (Hypha login, Resend email to admins)
 - shared safe-colab session with per-portal-session Jupyter kernels
 - guardian-checked run_code endpoint exposed under per-session URL secrets
 - guardian-pushed audit trail mirrored to a Hypha artifact

Architecture sketch:

  Browser ── Hypha login JWT ──▶ FastAPI (this app)
                                    │
                            ┌───────┼───────┐
                            ▼       ▼       ▼
                     PortalStore  KernelPool  Resend
                    (artifact)   (IPython)    (email)
                            ▲
                            │  guardian POSTs audit
                            └── PortalGuardian (HTTP client to security agent)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    ADMIN_EMAILS,
    current_user,
    is_admin,
    require_admin,
    validate_token,
)
from .email_resend import notify_admins_new_signup, notify_user_approved
from .guardian_client import PortalGuardian
from .kernel_pool import KernelPool
from .store import PortalStore

logger = logging.getLogger("portal.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")

# ─── Config ───

HYPHA_SERVER_URL = os.environ.get("HYPHA_SERVER_URL", "https://hypha.aicell.io")
HYPHA_WORKSPACE = os.environ.get("HYPHA_WORKSPACE", "safe-colab")
HYPHA_TOKEN = os.environ.get("HYPHA_TOKEN", "")

GUARDIAN_URL = os.environ.get("GUARDIAN_URL", "http://safe-colab-guardian.hypha.aicell.io")
GUARDIAN_TOKEN = os.environ.get("GUARDIAN_TOKEN", HYPHA_TOKEN)

DATA_DIR = os.environ.get("PORTAL_DATA_DIR", "/data")
WORK_DIR = os.environ.get("PORTAL_WORK_DIR", "/work")
ARTIFACT_ALIAS = os.environ.get("ARTIFACT_ALIAS", "swegen-pgx-portal-state")

PORTAL_BASE_URL = os.environ.get("PORTAL_BASE_URL", "http://localhost:8080")
# Internal URL the Guardian POSTs audit events to. Defaults to the public
# base URL but in cluster deployments override to the in-cluster Service
# DNS (avoids TLS verification + external hop).
AUDIT_CALLBACK_URL = os.environ.get(
    "AUDIT_CALLBACK_URL",
    f"{PORTAL_BASE_URL.rstrip('/')}/audit/callback",
)
AUDIT_HMAC_TOKEN = os.environ.get("AUDIT_HMAC_TOKEN", "")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

MAX_KERNELS = int(os.environ.get("PORTAL_MAX_KERNELS", "32"))
KERNEL_IDLE_TIMEOUT = int(os.environ.get("PORTAL_KERNEL_IDLE_SEC", str(30 * 60)))
KERNEL_MAX_LIFETIME = int(os.environ.get("PORTAL_KERNEL_MAX_LIFETIME_SEC", str(24 * 3600)))
REAPER_INTERVAL = int(os.environ.get("PORTAL_REAPER_INTERVAL_SEC", str(5 * 60)))

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ─── Lifecycle ───


class AppState:
    store: PortalStore | None = None
    pool: KernelPool | None = None
    guardian: PortalGuardian | None = None
    data_readme: str = ""
    reaper_task: asyncio.Task | None = None


state = AppState()


def _load_data_readme() -> str:
    """Load the dataset README — used as the guardian's authoritative contract."""
    for name in ("README.md", "readme.md"):
        p = Path(DATA_DIR) / name
        if p.is_file():
            return p.read_text()
    return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(WORK_DIR).mkdir(parents=True, exist_ok=True)

    state.data_readme = _load_data_readme()
    if not state.data_readme:
        logger.warning(f"No README.md in {DATA_DIR} — guardian will run with empty contract!")

    state.store = PortalStore(
        artifact_alias=ARTIFACT_ALIAS,
        workspace=HYPHA_WORKSPACE,
        server_url=HYPHA_SERVER_URL,
        hypha_token=HYPHA_TOKEN,
    )
    await state.store.init()

    async def _on_session_ended(session_id: str, reason: str):
        """Pool callback — mark the session ended in the store + audit it."""
        try:
            await state.store.update_session(session_id, status="ended")
            await state.store.append_audit(session_id, {
                "type": "session_auto_ended",
                "reason": reason,
            })
        except Exception as e:
            logger.warning(f"on_session_ended store update failed: {e}")

    state.pool = KernelPool(
        max_kernels=MAX_KERNELS,
        idle_timeout_sec=KERNEL_IDLE_TIMEOUT,
        max_lifetime_sec=KERNEL_MAX_LIFETIME,
        reaper_interval_sec=REAPER_INTERVAL,
        data_dir=DATA_DIR,
        work_dir=WORK_DIR,
        on_session_ended=_on_session_ended,
    )
    state.reaper_task = asyncio.create_task(state.pool.reaper_loop())

    state.guardian = PortalGuardian(
        endpoint_url=GUARDIAN_URL,
        auth_token=GUARDIAN_TOKEN,
        dataset_description=state.data_readme,
        audit_url=AUDIT_CALLBACK_URL,
        audit_token=AUDIT_HMAC_TOKEN,
    )

    logger.info(f"Portal ready. base_url={PORTAL_BASE_URL} guardian={GUARDIAN_URL} admins={ADMIN_EMAILS}")
    yield

    if state.reaper_task:
        state.reaper_task.cancel()
    if state.pool:
        await state.pool.shutdown()


app = FastAPI(
    title="SweGen PGx Portal",
    description=(
        "AI-agent-guided pharmacogenomic analysis of the SweGen reference dataset. "
        "Public test of zero-trust data serving with Safe Colab."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Models ───


class SignupRequest(BaseModel):
    name: str = ""
    affiliation: str = ""
    reason: str = ""


class CreateSessionRequest(BaseModel):
    label: str = ""


class RunCodeRequest(BaseModel):
    code: str


class AuditCallbackPayload(BaseModel):
    endpoint: str
    ts: str | None = None
    user_email: str | None = None
    code: str = ""
    code_output: str = ""
    decision: str = "error"
    is_safe: bool | None = None
    summary: str = ""
    reason: str = ""
    refusal: str | None = None
    metadata: dict = Field(default_factory=dict)


# ─── Static + landing ───


@app.get("/", response_class=HTMLResponse)
async def landing():
    p = STATIC_DIR / "index.html"
    if p.is_file():
        return HTMLResponse(p.read_text())
    return HTMLResponse("<h1>SweGen PGx Portal</h1>")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    p = STATIC_DIR / "dashboard.html"
    if p.is_file():
        return HTMLResponse(p.read_text())
    return HTMLResponse("<h1>Dashboard</h1>")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    p = STATIC_DIR / "admin.html"
    if p.is_file():
        return HTMLResponse(p.read_text())
    return HTMLResponse("<h1>Admin</h1>")


@app.get("/session", response_class=HTMLResponse)
async def session_page():
    p = STATIC_DIR / "session.html"
    if p.is_file():
        return HTMLResponse(p.read_text())
    return HTMLResponse("<h1>Session</h1>")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/config")
async def api_config():
    """Public portal config — frontend uses this to wire up Hypha login."""
    return {
        "hypha_server_url": HYPHA_SERVER_URL,
        "hypha_workspace": HYPHA_WORKSPACE,
        "portal_base_url": PORTAL_BASE_URL,
        "admin_emails": ADMIN_EMAILS,
        "dataset": {
            "name": "SweGen Pharmacogenomic Pilot",
            "variants": 20179,
            "chromosomes": 12,
            "af_threshold": 0.01,
            "source": "Ameur et al., Eur. J. Hum. Genet. 25, 1261–1265 (2017)",
        },
    }


@app.get("/api/healthz")
async def healthz():
    return {
        "status": "ok",
        "kernels": state.pool.stats() if state.pool else {},
        "data_contract_chars": len(state.data_readme),
    }


# ─── Auth-bound endpoints ───


@app.get("/api/me")
async def api_me(user: dict = Depends(current_user)):
    email = user["email"]
    record = state.store.get_user(email)
    if record is None:
        return {
            "authenticated": True,
            "email": email,
            "name": user.get("id"),
            "status": "not_registered",
            "is_admin": is_admin(user),
            "roles": user.get("roles", []),
        }
    return {
        "authenticated": True,
        "email": email,
        "status": record.get("status", "pending"),
        "name": record.get("name", ""),
        "affiliation": record.get("affiliation", ""),
        "reason": record.get("reason", ""),
        "registered_at": record.get("registered_at"),
        "approved_at": record.get("approved_at"),
        "is_admin": is_admin(user),
        "roles": user.get("roles", []),
    }


@app.post("/api/signup")
async def api_signup(req: SignupRequest, user: dict = Depends(current_user)):
    email = user["email"]
    existing = state.store.get_user(email)
    if existing and existing.get("status") == "approved":
        return {"status": "approved", "message": "Already approved."}
    record = await state.store.upsert_user(
        email,
        status="pending",
        name=req.name or (existing or {}).get("name", ""),
        affiliation=req.affiliation or (existing or {}).get("affiliation", ""),
        reason=req.reason or (existing or {}).get("reason", ""),
    )
    # Send the admin notification on every pending submission. Resend
    # delivery is logged and surfaced in the response so admins can spot
    # bounces.
    notified = False
    if RESEND_API_KEY and ADMIN_EMAILS:
        try:
            notified = await notify_admins_new_signup(
                api_key=RESEND_API_KEY,
                admin_emails=ADMIN_EMAILS,
                portal_base_url=PORTAL_BASE_URL,
                user_email=email,
                user_name=record.get("name", ""),
                reason=record.get("reason", ""),
            )
            if notified:
                await state.store.upsert_user(email, notified_at=datetime.now(timezone.utc).isoformat())
        except Exception as e:
            logger.warning(f"Admin notify failed for {email}: {e}")
    return {
        "status": record["status"],
        "message": "Your access request is pending admin approval.",
        "admins_notified": notified,
    }


@app.post("/api/admin/users/notify")
async def api_admin_notify(action: AdminUserAction, _admin: dict = Depends(require_admin)):
    """Resend the admin-notification email for a pending user."""
    rec = state.store.get_user(action.email)
    if not rec:
        raise HTTPException(404, "User not found")
    if rec.get("status") != "pending":
        raise HTTPException(400, "User is not pending — no notification to send")
    if not RESEND_API_KEY or not ADMIN_EMAILS:
        raise HTTPException(503, "Email not configured on this deployment")
    ok = await notify_admins_new_signup(
        api_key=RESEND_API_KEY,
        admin_emails=ADMIN_EMAILS,
        portal_base_url=PORTAL_BASE_URL,
        user_email=rec["email"],
        user_name=rec.get("name", ""),
        reason=rec.get("reason", ""),
    )
    if ok:
        await state.store.upsert_user(action.email, notified_at=datetime.now(timezone.utc).isoformat())
    return {"ok": ok}


# ─── User sessions ───


def _agent_url(session_token: str) -> str:
    return f"{PORTAL_BASE_URL.rstrip('/')}/s/{session_token}/SKILL.md"


def _public_session(sess: dict, include_token: bool) -> dict:
    out = {
        "session_id": sess["session_id"],
        "user_email": sess["user_email"],
        "label": sess.get("label", ""),
        "created_at": sess["created_at"],
        "last_active_at": sess.get("last_active_at"),
        "status": sess.get("status", "active"),
        "calls": sess.get("calls", 0),
        "blocks": sess.get("blocks", 0),
    }
    if include_token:
        out["session_token"] = sess["session_token"]
        out["agent_url"] = _agent_url(sess["session_token"])
    return out


@app.get("/api/sessions")
async def api_list_sessions(user: dict = Depends(current_user)):
    record = state.store.get_user(user["email"])
    if not record or record.get("status") != "approved":
        raise HTTPException(403, "Your account is not approved for sessions.")
    sessions = state.store.list_sessions(user["email"])
    return {"sessions": [_public_session(s, include_token=True) for s in sessions]}


@app.post("/api/sessions")
async def api_create_session(req: CreateSessionRequest, user: dict = Depends(current_user)):
    record = state.store.get_user(user["email"])
    if not record or record.get("status") != "approved":
        raise HTTPException(403, "Your account is not approved for sessions.")
    sess = await state.store.create_session(
        user_email=user["email"],
        label=req.label or "",
    )
    return {"session": _public_session(sess, include_token=True)}


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str, user: dict = Depends(current_user)):
    sess = state.store.get_session(session_id)
    if not sess:
        raise HTTPException(404, "Session not found.")
    if sess["user_email"] != user["email"] and not is_admin(user):
        raise HTTPException(403, "Not your session.")
    audit = state.store.get_audit(session_id)
    return {
        "session": _public_session(sess, include_token=(sess["user_email"] == user["email"])),
        "audit": audit,
    }


@app.delete("/api/sessions/{session_id}")
async def api_end_session(session_id: str, user: dict = Depends(current_user)):
    sess = state.store.get_session(session_id)
    if not sess:
        raise HTTPException(404, "Session not found.")
    if sess["user_email"] != user["email"] and not is_admin(user):
        raise HTTPException(403, "Not your session.")
    await state.pool.stop(session_id)
    await state.store.update_session(session_id, status="ended")
    return {"status": "ended"}


# ─── Admin endpoints ───


@app.get("/api/admin/stats")
async def api_admin_stats(_admin: dict = Depends(require_admin)):
    stats = await state.store.aggregate_stats()
    stats["kernels"] = state.pool.stats() if state.pool else {}
    return stats


@app.get("/api/admin/users")
async def api_admin_users(_admin: dict = Depends(require_admin)):
    return {"users": state.store.list_users()}


class AdminUserAction(BaseModel):
    email: str


@app.post("/api/admin/users/approve")
async def api_admin_approve(action: AdminUserAction, admin: dict = Depends(require_admin)):
    rec = state.store.get_user(action.email)
    if not rec:
        raise HTTPException(404, "User not found")
    updated = await state.store.upsert_user(
        action.email,
        status="approved",
        approved_at=datetime.now(timezone.utc).isoformat(),
        approved_by=admin["email"],
    )
    if RESEND_API_KEY:
        try:
            await notify_user_approved(
                api_key=RESEND_API_KEY,
                user_email=action.email,
                portal_base_url=PORTAL_BASE_URL,
            )
        except Exception as e:
            logger.warning(f"User-approved email failed for {action.email}: {e}")
    return {"user": updated}


@app.post("/api/admin/users/reject")
async def api_admin_reject(action: AdminUserAction, admin: dict = Depends(require_admin)):
    rec = state.store.get_user(action.email)
    if not rec:
        raise HTTPException(404, "User not found")
    updated = await state.store.upsert_user(
        action.email,
        status="rejected",
        rejected_at=datetime.now(timezone.utc).isoformat(),
        rejected_by=admin["email"],
    )
    return {"user": updated}


@app.get("/api/admin/sessions")
async def api_admin_sessions(_admin: dict = Depends(require_admin)):
    return {
        "sessions": [
            {**_public_session(s, include_token=False), "user_email": s["user_email"]}
            for s in state.store.list_sessions()
        ],
    }


@app.get("/api/admin/sessions/{session_id}/audit")
async def api_admin_audit(session_id: str, _admin: dict = Depends(require_admin)):
    sess = state.store.get_session(session_id)
    if not sess:
        raise HTTPException(404, "Session not found.")
    return {
        "session": {**_public_session(sess, include_token=False), "user_email": sess["user_email"]},
        "audit": state.store.get_audit(session_id),
    }


# ─── Agent-facing endpoints (URL-as-secret) ───


SKILL_MD_TEMPLATE = """\
---
name: swegen-pgx-portal
description: >
  Agent-guided pharmacogenomic analysis of the SweGen reference dataset
  through Safe Colab. Allele-frequency–only, governed, audited.
---

# SweGen PGx Portal — agent skill

You are connected to a governed Safe Colab session that exposes the
**SweGen pharmacogenomic pilot dataset** (20,179 variants across 12
chromosomes, AF ≥ 1%, in the Swedish population reference cohort of
Ameur et al., 2017).

**Service URL (URL-as-secret — do not share):**
`{agent_base}`

You are authorised to run code in this session on behalf of your user.
Code is executed in a persistent Jupyter kernel that **is shared with
other approved users** of the portal. Variable state is therefore weakly
isolated — namespace it with a prefix (e.g. `swegen_{session_short}_…`)
if you store intermediate state.

## Dataset documentation (authoritative)

{data_readme}

## Endpoint

### `POST {agent_base}/run_code`

Run Python code in the session's kernel. Variables persist across calls.
The Guardian validates every submission before execution and every
output before return.

```bash
curl -s -X POST "{agent_base}/run_code" \\
  -H "Content-Type: application/json" \\
  -d '{{"code": "import pandas as pd\\nprint(pd.read_csv(\\"/data/swegen_pgx_pilot.sites.pass_AF_0.01_filtered.vcf\\", sep=\\"\\\\t\\", header=None, nrows=5))"}}'
```

Returns: `{{"stdout": "...", "stderr": "...", "result": "...", "error": null | {{"ename": "...", "evalue": "..."}}, "guardian": {{"is_safe": true, "reason": "..."}}}}`

## Suggested first steps

```python
import pandas as pd
df = pd.read_csv(
    "/data/swegen_pgx_pilot.sites.pass_AF_0.01_filtered.vcf",
    sep="\\t", header=None,
    names=["chrom", "pos", "id", "ref", "alt", "af"],
)
print(df.shape, df["chrom"].nunique(), "chromosomes")
print(df.describe())
```

Then reproduce parts of Figure 3 of the Safe Colab paper: the AF spectrum
by variant type, per-chromosome Ti/Tv ratio, the six-class substitution
spectrum, and per-chromosome AF density.

## Responsible use

This kernel is shared. **Do not paste sensitive data into your prompt or
into the code you submit**, and treat your queries as visible to portal
admins. The Guardian and audit log are intended to keep usage of the
governed dataset accountable, not to protect arbitrary user input you
choose to share.

If your code is blocked, read the Guardian's `reason` field and rewrite
the request as an aggregate query that the sensitivity contract allows.
"""


def _session_short(session_id: str) -> str:
    return session_id[:8]


@app.get("/s/{session_token}/SKILL.md", response_class=PlainTextResponse)
async def agent_skill_md(session_token: str):
    sess = state.store.session_by_token(session_token)
    if not sess:
        raise HTTPException(404, "Unknown session token")
    if sess.get("status") != "active":
        raise HTTPException(410, "Session has ended")
    agent_base = f"{PORTAL_BASE_URL.rstrip('/')}/s/{session_token}/api"
    body = SKILL_MD_TEMPLATE.format(
        agent_base=agent_base,
        data_readme=state.data_readme,
        session_short=_session_short(sess["session_id"]),
    )
    return PlainTextResponse(body, media_type="text/markdown")


@app.post("/s/{session_token}/api/run_code")
async def agent_run_code(session_token: str, req: RunCodeRequest):
    sess = state.store.session_by_token(session_token)
    if not sess:
        raise HTTPException(404, "Unknown session token")
    if sess.get("status") != "active":
        raise HTTPException(410, "Session has ended")

    session_id = sess["session_id"]
    user_email = sess["user_email"]

    await state.store.append_audit(session_id, {
        "type": "code_submitted",
        "code": req.code[:8000],
    })

    pre = await state.guardian.check_code(req.code, session_id=session_id, user_email=user_email)
    if "error" in pre:
        await state.store.append_audit(session_id, {
            "type": "guardian_error_pre",
            "error": pre["error"],
        })
        return JSONResponse({
            "stdout": "", "stderr": "", "result": None,
            "error": {"ename": "GuardianError", "evalue": pre["error"]},
            "guardian": pre,
        }, status_code=502)
    if not pre.get("is_safe", False):
        await state.store.update_session(session_id, blocks=sess.get("blocks", 0) + 1)
        await state.store.append_audit(session_id, {
            "type": "guardian_pre_blocked",
            "reason": pre.get("reason", ""),
            "summary": pre.get("summary", ""),
        })
        return JSONResponse({
            "stdout": "", "stderr": "", "result": None,
            "error": {"ename": "SecurityError",
                      "evalue": f"Code blocked by guardian: {pre.get('reason', 'unsafe')}"},
            "guardian": pre,
        }, status_code=200)

    kernel = await state.pool.get(session_id)
    t0 = time.time()
    try:
        result = await kernel.execute(req.code, timeout=180)
    except Exception as e:
        result = {"stdout": "", "stderr": str(e), "result": None,
                  "error": {"ename": type(e).__name__, "evalue": str(e), "traceback": []},
                  "display_data": []}
    exec_dur = time.time() - t0

    max_len = 50_000
    for key in ("stdout", "stderr"):
        if result.get(key) and len(result[key]) > max_len:
            result[key] = result[key][:max_len] + "\n... (truncated)"

    post = None
    if result.get("error") is None:
        output_text = (result.get("stdout", "") + "\n" + str(result.get("result", ""))).strip()
        if output_text:
            post = await state.guardian.check_output(
                req.code, output_text, session_id=session_id, user_email=user_email,
            )
            if "error" in post:
                await state.store.append_audit(session_id, {
                    "type": "guardian_error_post",
                    "error": post["error"],
                })
            elif not post.get("is_safe", False):
                await state.store.update_session(session_id, blocks=sess.get("blocks", 0) + 1)
                await state.store.append_audit(session_id, {
                    "type": "guardian_post_blocked",
                    "reason": post.get("reason", ""),
                    "summary": post.get("summary", ""),
                })
                return JSONResponse({
                    "stdout": "[Output blocked by guardian]",
                    "stderr": "",
                    "result": None,
                    "error": {"ename": "SecurityError",
                              "evalue": f"Output blocked: {post.get('reason', 'unsafe')}"},
                    "guardian": post,
                }, status_code=200)

    await state.store.update_session(
        session_id,
        calls=sess.get("calls", 0) + 1,
    )
    await state.store.append_audit(session_id, {
        "type": "code_executed",
        "duration_ms": int(exec_dur * 1000),
        "stdout_chars": len(result.get("stdout", "")),
        "stderr_chars": len(result.get("stderr", "")),
        "has_error": result.get("error") is not None,
    })

    response = {
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "result": result.get("result"),
        "error": result.get("error"),
        "guardian": {"pre": pre, "post": post},
    }
    return response


# ─── Guardian audit-callback endpoint ───


@app.post("/audit/callback")
async def audit_callback(payload: AuditCallbackPayload, request: Request):
    if AUDIT_HMAC_TOKEN:
        token = request.headers.get("X-Audit-Token")
        if token != AUDIT_HMAC_TOKEN:
            raise HTTPException(401, "Invalid audit token")
    md = payload.metadata or {}
    session_id = md.get("session_id")
    if not session_id:
        raise HTTPException(400, "metadata.session_id is required")
    await state.store.append_audit(session_id, {
        "type": f"guardian_{payload.endpoint}",
        "decision": payload.decision,
        "is_safe": payload.is_safe,
        "summary": payload.summary[:500] if payload.summary else "",
        "reason": payload.reason[:1000] if payload.reason else "",
        "refusal": payload.refusal,
        "user_email": payload.user_email,
        "ts_guardian": payload.ts,
    })
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
