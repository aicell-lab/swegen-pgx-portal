"""Hypha-artifact–backed primary store for portal state.

Stores three logical collections inside a single artifact (default alias
`swegen-pgx-portal-state`):

  users/{email_b64}.json      - user record (email, status, timestamps)
  sessions/{session_id}.json  - session record (user, kernel info, totals)
  audit/{session_id}/<ts>_<short>.json - one file per audit event

All writes are write-through: in-memory cache + put_file + commit. On
startup we scan the artifact and populate the cache. The portal pod owns
the only writer, so we don't worry about cross-process write conflicts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("portal.store")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _email_key(email: str) -> str:
    return base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip("=")


def _decode_email_key(key: str) -> str:
    pad = "=" * (-len(key) % 4)
    return base64.urlsafe_b64decode(key + pad).decode()


class PortalStore:
    """Hypha-artifact-backed store for users, sessions, and audit events."""

    def __init__(
        self,
        artifact_alias: str = "swegen-pgx-portal-state",
        workspace: str | None = None,
        server_url: str = "https://hypha.aicell.io",
        hypha_token: str = "",
    ):
        self.alias = artifact_alias
        self.workspace = workspace
        self.server_url = server_url.rstrip("/")
        self.hypha_token = hypha_token
        self._server = None
        self._am = None
        self._artifact_id: str | None = None
        self._lock = asyncio.Lock()
        self._users: dict[str, dict] = {}
        self._sessions: dict[str, dict] = {}
        self._audit: dict[str, list[dict]] = {}

    async def init(self):
        """Connect to Hypha, ensure the artifact exists, hydrate caches."""
        from hypha_rpc import connect_to_server

        connect_kwargs: dict[str, Any] = {
            "server_url": self.server_url,
            "token": self.hypha_token,
        }
        if self.workspace:
            connect_kwargs["workspace"] = self.workspace
        self._server = await connect_to_server(connect_kwargs)
        self._am = await self._server.get_service("public/artifact-manager")

        try:
            existing = await self._am.read(artifact_id=self.alias)
            self._artifact_id = existing["id"] if isinstance(existing, dict) else existing.id
            logger.info(f"Using existing portal-state artifact: {self._artifact_id}")
        except Exception:
            created = await self._am.create(
                alias=self.alias,
                type="dataset",
                manifest={
                    "name": "SweGen PGx Portal — state",
                    "description": "Users, sessions and audit events for the swegen-pgx portal.",
                },
                config={"permissions": {"@": "rw+"}},
            )
            self._artifact_id = created["id"] if isinstance(created, dict) else created.id
            logger.info(f"Created portal-state artifact: {self._artifact_id}")

        await self._hydrate()

    async def _stage_put(self, path: str, content: bytes, content_type: str = "application/json"):
        """Stage a file, upload via presigned URL, commit."""
        try:
            await self._am.edit(artifact_id=self._artifact_id, stage=True)
        except Exception:
            pass
        put_url = await self._am.put_file(artifact_id=self._artifact_id, file_path=path)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.put(
                put_url,
                content=content,
                headers={"Content-Type": content_type, "Content-Length": str(len(content))},
            )
            resp.raise_for_status()
        await self._am.commit(artifact_id=self._artifact_id)

    async def _get_text(self, path: str) -> str | None:
        try:
            url = await self._am.get_file(artifact_id=self._artifact_id, file_path=path)
        except Exception as e:
            logger.warning(f"get_file({path}) failed: {e}")
            return None
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code != 200:
                return None
            return resp.text

    async def _list(self, prefix: str = "") -> list[dict]:
        try:
            return await self._am.list_files(artifact_id=self._artifact_id, dir_path=prefix or None)
        except Exception as e:
            logger.warning(f"list_files({prefix}) failed: {e}")
            return []

    async def _hydrate(self):
        """Populate in-memory caches from the artifact."""
        async def _load_dir(prefix: str, target: dict, key_field: str):
            entries = await self._list(prefix)
            for entry in entries:
                if isinstance(entry, dict):
                    name = entry.get("name", "")
                    if entry.get("type") == "directory":
                        continue
                else:
                    name = str(entry)
                if not name or not name.endswith(".json"):
                    continue
                text = await self._get_text(f"{prefix}/{name}")
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                    target[obj[key_field]] = obj
                except Exception as e:
                    logger.warning(f"Failed to load {prefix}/{name}: {e}")

        await _load_dir("users", self._users, "email")
        await _load_dir("sessions", self._sessions, "session_id")

        sessions_entries = await self._list("audit")
        for entry in sessions_entries:
            name = entry.get("name") if isinstance(entry, dict) else str(entry)
            if not name:
                continue
            session_id = name.rstrip("/")
            events = []
            for ev_entry in await self._list(f"audit/{session_id}"):
                ev_name = ev_entry.get("name") if isinstance(ev_entry, dict) else str(ev_entry)
                if not ev_name or not ev_name.endswith(".json"):
                    continue
                text = await self._get_text(f"audit/{session_id}/{ev_name}")
                if text:
                    try:
                        events.append(json.loads(text))
                    except Exception:
                        pass
            events.sort(key=lambda e: e.get("ts", ""))
            self._audit[session_id] = events

        logger.info(
            f"Store hydrated: {len(self._users)} users, "
            f"{len(self._sessions)} sessions, "
            f"{sum(len(v) for v in self._audit.values())} audit events"
        )

    async def upsert_user(self, email: str, **fields):
        async with self._lock:
            email = email.lower()
            user = self._users.get(email, {
                "email": email,
                "status": "pending",
                "registered_at": _now_iso(),
            })
            user.update(fields)
            user["email"] = email
            user["updated_at"] = _now_iso()
            self._users[email] = user
            await self._stage_put(
                f"users/{_email_key(email)}.json",
                json.dumps(user).encode(),
            )
            return user

    def get_user(self, email: str) -> dict | None:
        return self._users.get(email.lower())

    def list_users(self) -> list[dict]:
        return sorted(self._users.values(), key=lambda u: u.get("registered_at", ""))

    async def create_session(self, user_email: str, **fields) -> dict:
        async with self._lock:
            session_id = uuid.uuid4().hex
            session_token = uuid.uuid4().hex + uuid.uuid4().hex
            now = _now_iso()
            sess = {
                "session_id": session_id,
                "session_token": session_token,
                "user_email": user_email.lower(),
                "created_at": now,
                "last_active_at": now,
                "status": "active",
                "calls": 0,
                "blocks": 0,
                **fields,
            }
            self._sessions[session_id] = sess
            self._audit[session_id] = []
            await self._stage_put(
                f"sessions/{session_id}.json",
                json.dumps(sess).encode(),
            )
            return sess

    async def update_session(self, session_id: str, **fields):
        async with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return None
            sess.update(fields)
            sess["last_active_at"] = _now_iso()
            await self._stage_put(
                f"sessions/{session_id}.json",
                json.dumps(sess).encode(),
            )
            return sess

    def get_session(self, session_id: str) -> dict | None:
        return self._sessions.get(session_id)

    def session_by_token(self, token: str) -> dict | None:
        for sess in self._sessions.values():
            if sess.get("session_token") == token:
                return sess
        return None

    def list_sessions(self, user_email: str | None = None) -> list[dict]:
        sessions = list(self._sessions.values())
        if user_email:
            sessions = [s for s in sessions if s.get("user_email") == user_email.lower()]
        return sorted(sessions, key=lambda s: s.get("created_at", ""), reverse=True)

    async def append_audit(self, session_id: str, event: dict):
        """Append a single audit event under audit/{session_id}/."""
        async with self._lock:
            ts = event.get("ts") or _now_iso()
            ev_id = event.get("event_id") or uuid.uuid4().hex[:12]
            event = {**event, "ts": ts, "event_id": ev_id, "session_id": session_id}
            self._audit.setdefault(session_id, []).append(event)
            fname = f"{ts.replace(':', '-')}_{ev_id}.json"
            await self._stage_put(
                f"audit/{session_id}/{fname}",
                json.dumps(event).encode(),
            )

    def get_audit(self, session_id: str) -> list[dict]:
        return list(self._audit.get(session_id, []))

    async def aggregate_stats(self) -> dict:
        """Return high-level stats for the admin dashboard."""
        total_calls = sum(len(events) for events in self._audit.values())
        total_blocks = sum(
            1 for events in self._audit.values()
            for e in events
            if e.get("decision") == "blocked"
        )
        pending = [u for u in self._users.values() if u.get("status") == "pending"]
        approved = [u for u in self._users.values() if u.get("status") == "approved"]
        return {
            "users_total": len(self._users),
            "users_pending": len(pending),
            "users_approved": len(approved),
            "sessions_total": len(self._sessions),
            "audit_events_total": total_calls,
            "blocks_total": total_blocks,
        }
