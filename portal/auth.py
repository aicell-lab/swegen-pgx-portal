"""Hypha JWT validation + admin check.

Mirrors the pattern used by the security-agent app: try a fast local JWT
decode first, then verify against Hypha's parse_token endpoint when
possible. Caches results for 5 minutes.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("portal.auth")

HYPHA_SERVER_URL = os.environ.get("HYPHA_SERVER_URL", "https://hypha.aicell.io")
ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.environ.get(
        "ADMIN_EMAILS",
        "adam.ameur@igp.uu.se,joanna.hard@scilifelab.se,wei.ouyang@scilifelab.se",
    ).split(",")
    if e.strip()
]

_token_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 300
_bearer = HTTPBearer(auto_error=False)


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT structure")
    payload_b64 = parts[1]
    pad = len(payload_b64) % 4
    if pad:
        payload_b64 += "=" * (4 - pad)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


async def validate_token(token: str) -> dict:
    now = time.time()
    cached = _token_cache.get(token)
    if cached and now - cached[1] < _CACHE_TTL:
        return cached[0]

    try:
        payload = _decode_jwt_payload(token)
    except Exception:
        raise HTTPException(401, "Invalid token: cannot decode JWT")

    exp = payload.get("exp")
    if exp and time.time() > exp:
        raise HTTPException(401, "Token has expired")

    scope = payload.get("scope", "")
    ws_match = re.search(r"ws:([\w\-|]+)#", scope)
    info = {
        "id": payload.get("sub", "unknown"),
        "email": (payload.get("https://amun.ai/email") or "").lower(),
        "workspace": ws_match.group(1) if ws_match else "",
        "roles": payload.get("https://amun.ai/roles", []),
    }

    try:
        url = f"{HYPHA_SERVER_URL}/{info['workspace'] or 'public'}/services/ws/parse_token"
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                url, params={"token": token},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                server_info = resp.json()
                if isinstance(server_info, dict) and server_info.get("id"):
                    info["id"] = server_info.get("id", info["id"])
                    info["email"] = (server_info.get("email", info["email"]) or "").lower()
                    info["roles"] = server_info.get("roles", info["roles"])
    except Exception as e:
        logger.debug(f"Server-side token validation skipped: {e}")

    if not info["email"]:
        raise HTTPException(401, "Token has no email claim")

    _token_cache[token] = (info, now)
    return info


async def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Validate via Authorization: Bearer header or 'portal_token' cookie."""
    token = credentials.credentials if credentials else None
    if not token:
        token = request.cookies.get("portal_token")
    if not token:
        raise HTTPException(401, "Not authenticated. Please sign in with Hypha.")
    return await validate_token(token)


def is_admin(user: dict) -> bool:
    return (user.get("email") or "").lower() in ADMIN_EMAILS


async def require_admin(user: dict = Depends(current_user)) -> dict:
    if not is_admin(user):
        raise HTTPException(403, "Admin access required")
    return user
