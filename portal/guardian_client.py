"""Thin guardian client used by the portal.

Wraps the two security-check HTTP endpoints and adds the `audit_callback`
field so the Guardian POSTs its decision independently to the portal's
audit endpoint. This produces a Guardian-signed trail that does not pass
through the portal's own logging code.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("portal.guardian_client")

# Returned message when the Guardian's underlying LLM provider is out of
# credit / rate-limited. We surface this distinctly from a normal
# "your code violates the contract" error so the agent doesn't think
# the user did something wrong.
QUOTA_ERROR_MESSAGE = (
    "The portal's Guardian service is temporarily unavailable because its "
    "LLM provider quota is exhausted. Portal admins are aware. Please retry "
    "in a few minutes, or contact wei.ouyang@scilifelab.se if it persists."
)


def _classify_guardian_error(text: str) -> str | None:
    """If `text` looks like an OpenAI quota / billing / rate-limit error,
    return the friendly QUOTA_ERROR_MESSAGE. Otherwise return None."""
    if not text:
        return None
    low = text.lower()
    for marker in (
        "insufficient_quota",
        "exceeded your current quota",
        "billing",
        "rate limit",
        "rate_limit_exceeded",
        "you exceeded",
    ):
        if marker in low:
            return QUOTA_ERROR_MESSAGE
    return None


class PortalGuardian:
    def __init__(
        self,
        endpoint_url: str,
        auth_token: str,
        dataset_description: str,
        audit_url: str,
        audit_token: str,
    ):
        self.endpoint_url = endpoint_url.rstrip("/")
        self.dataset_description = dataset_description
        self.audit_url = audit_url
        self.audit_token = audit_token
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
        }

    def _audit_block(self, session_id: str, user_email: str) -> dict[str, Any]:
        if not self.audit_url:
            return None
        return {
            "url": self.audit_url,
            "token": self.audit_token,
            "metadata": {
                "session_id": session_id,
                "user_email": user_email,
            },
        }

    async def check_code(self, code: str, session_id: str, user_email: str) -> dict:
        payload = {
            "code": code,
            "dataset_description": self.dataset_description,
            "audit_callback": self._audit_block(session_id, user_email),
        }
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{self.endpoint_url}/ensure_code_secure",
                    json=payload,
                    headers=self._headers,
                )
        except Exception as e:
            return {"error": f"Guardian unreachable: {e}", "kind": "unreachable"}
        if resp.status_code != 200:
            body = resp.text[:400]
            friendly = _classify_guardian_error(body)
            if friendly:
                return {"error": friendly, "kind": "quota"}
            return {"error": f"Guardian {resp.status_code}: {body[:200]}", "kind": "http_error"}
        result = resp.json()
        refusal = result.get("refusal")
        if refusal:
            friendly = _classify_guardian_error(refusal)
            if friendly:
                return {"error": friendly, "kind": "quota"}
            return {"error": refusal, "kind": "refusal"}
        return result.get("parsed") or {}

    async def check_output(self, code: str, output: str, session_id: str, user_email: str) -> dict:
        payload = {
            "code": code,
            "code_output": output,
            "dataset_description": self.dataset_description,
            "audit_callback": self._audit_block(session_id, user_email),
        }
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{self.endpoint_url}/ensure_output_secure",
                    json=payload,
                    headers=self._headers,
                )
        except Exception as e:
            return {"error": f"Guardian unreachable: {e}", "kind": "unreachable"}
        if resp.status_code != 200:
            body = resp.text[:400]
            friendly = _classify_guardian_error(body)
            if friendly:
                return {"error": friendly, "kind": "quota"}
            return {"error": f"Guardian {resp.status_code}: {body[:200]}", "kind": "http_error"}
        result = resp.json()
        refusal = result.get("refusal")
        if refusal:
            friendly = _classify_guardian_error(refusal)
            if friendly:
                return {"error": friendly, "kind": "quota"}
            return {"error": refusal, "kind": "refusal"}
        return result.get("parsed") or {}
