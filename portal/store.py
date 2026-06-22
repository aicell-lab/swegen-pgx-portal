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
        # Separate lock so a stuck RPC-call holding _lock doesn't block
        # the heartbeat / reconnect path.
        self._conn_lock = asyncio.Lock()
        # Timestamp of the most recent successful RPC call (or heartbeat
        # ping). `/api/healthz?deep=true` consults this to decide if the
        # store's connection is alive — much cheaper than firing a real
        # RPC call on every readiness probe.
        self._last_ok_at: float = 0.0
        self._users: dict[str, dict] = {}
        self._sessions: dict[str, dict] = {}
        self._audit: dict[str, list[dict]] = {}
        # Reports keyed by report_id. Each value is the manifest (metadata
        # only). The HTML body lives under reports/{report_id}/report.html
        # and is fetched on demand by `get_report_html`.
        self._reports: dict[str, dict] = {}

    async def init(self):
        """Connect to Hypha, ensure the artifact exists, hydrate caches."""
        await self._connect()
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
        self._mark_ok()
        await self._hydrate()

    # ── Connection management ──────────────────────────────────────────

    def _mark_ok(self):
        self._last_ok_at = time.time()

    def seconds_since_ok(self) -> float | None:
        """Seconds since the last confirmed-healthy Hypha call. None if
        the store has never made a successful call."""
        if not self._last_ok_at:
            return None
        return time.time() - self._last_ok_at

    async def _connect(self):
        """(Re)open the hypha-rpc server + artifact-manager handles.

        Long-lived hypha-rpc connections can go half-alive — TCP looks
        connected, but RPC methods time out. This helper closes the old
        handle and opens a fresh one. Holds `_conn_lock` so concurrent
        retries collapse onto one reconnect, but it does NOT hold
        `_lock`, so other writers don't block on a slow reconnect.
        """
        from hypha_rpc import connect_to_server

        async with self._conn_lock:
            # If another caller already reconnected since we entered this
            # method, reuse their handles instead of duplicating work.
            if self._am is not None and self._last_ok_at and time.time() - self._last_ok_at < 5:
                return
            old_server = self._server
            self._am = None
            self._server = None
            connect_kwargs: dict[str, Any] = {
                "server_url": self.server_url,
                "token": self.hypha_token,
            }
            if self.workspace:
                connect_kwargs["workspace"] = self.workspace
            try:
                self._server = await connect_to_server(connect_kwargs)
                self._am = await self._server.get_service("public/artifact-manager")
                logger.info("hypha connection (re)opened")
            except Exception as e:
                logger.error("hypha (re)connect failed: %s", e)
                raise
            if old_server is not None:
                try:
                    await old_server.disconnect()
                except Exception:
                    pass

    # Errors we treat as "the long-lived RPC handle is sick — reconnect
    # and retry." Anything else propagates as-is (legitimate server-side
    # errors, e.g. PermissionError, ValueError).
    _RECONNECT_EXC = (asyncio.TimeoutError, ConnectionError, OSError)

    async def _call(self, fn, *args, **kwargs):
        """Wrap one hypha-rpc method call with reconnect-on-stall retry.

        We catch the small set of exceptions that indicate the long-lived
        client is wedged, rebuild the connection, and retry exactly once.
        On the retry we look up `fn` again on the fresh `_am` so the new
        bound method goes through the new channel.
        """
        method_name = getattr(fn, "__name__", None) or getattr(fn, "name", "?")
        try:
            result = await fn(*args, **kwargs)
            self._mark_ok()
            return result
        except self._RECONNECT_EXC as e:
            logger.warning("hypha-rpc %s failed (%s) — reconnecting and retrying once",
                           method_name, type(e).__name__)
        except Exception as e:
            # TimeoutError lives under the asyncio namespace in Python 3.11
            # but hypha-rpc may surface it as a plain Exception with a
            # specific message ("Method call timed out: ..."). Sniff it.
            msg = str(e)
            if "timed out" in msg.lower() or "connection" in msg.lower():
                logger.warning("hypha-rpc %s timed out — reconnecting and retrying once: %s",
                               method_name, msg[:160])
            else:
                raise
        await self._connect()
        # Rebind the method on the fresh _am if it was an _am method.
        new_fn = fn
        if hasattr(self._am, method_name) and getattr(self._am, method_name, None) is not None:
            new_fn = getattr(self._am, method_name)
        result = await new_fn(*args, **kwargs)
        self._mark_ok()
        return result

    async def heartbeat_loop(self, interval_sec: int = 60):
        """Background task: ping Hypha every `interval_sec`, reconnect on failure.

        On its own this isn't enough — a stall could land between two
        pings. But combined with the retry-once-on-timeout in `_call()`
        and the deep healthz probe (which reads `seconds_since_ok` to
        decide if the pod is healthy), it keeps a stuck handle from
        ever serving traffic for long.
        """
        while True:
            try:
                await asyncio.sleep(interval_sec)
                await self._ping()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("heartbeat ping failed, will retry: %s", e)

    async def _ping(self):
        """Cheap RPC probe — confirms the handle is alive."""
        if self._am is None or self._artifact_id is None:
            return
        try:
            await self._call(self._am.list_files,
                             artifact_id=self._artifact_id, dir_path="users")
        except Exception as e:
            logger.warning("ping failed even after reconnect: %s", e)

    async def _stage_put(self, path: str, content: bytes, content_type: str = "application/json"):
        """Stage a file, upload via presigned URL, commit.

        Each hypha-rpc call (edit/put_file/commit) goes through `_call`
        independently so a stale handle is reconnected and retried at the
        exact call that stalled. The httpx PUT to the presigned URL is a
        plain HTTP upload, not an RPC call, so it keeps its own timeout.
        """
        try:
            await self._call(self._am.edit, artifact_id=self._artifact_id, stage=True)
        except Exception:
            pass
        put_url = await self._call(self._am.put_file, artifact_id=self._artifact_id, file_path=path)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.put(
                put_url,
                content=content,
                headers={"Content-Type": content_type, "Content-Length": str(len(content))},
            )
            resp.raise_for_status()
        await self._call(self._am.commit, artifact_id=self._artifact_id)

    async def _get_text(self, path: str) -> str | None:
        try:
            url = await self._call(self._am.get_file, artifact_id=self._artifact_id, file_path=path)
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
            return await self._call(self._am.list_files, artifact_id=self._artifact_id, dir_path=prefix or None)
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

        # Reports — each report has its own subdirectory under reports/
        # with a manifest.json + report.html.
        for entry in await self._list("reports"):
            if isinstance(entry, dict):
                name = entry.get("name", "")
                if entry.get("type") != "directory":
                    continue
            else:
                name = str(entry).rstrip("/")
            if not name:
                continue
            text = await self._get_text(f"reports/{name}/manifest.json")
            if not text:
                continue
            try:
                manifest = json.loads(text)
                self._reports[manifest["report_id"]] = manifest
            except Exception as e:
                logger.warning(f"Failed to load report {name}: {e}")

        logger.info(
            f"Store hydrated: {len(self._users)} users, "
            f"{len(self._sessions)} sessions, "
            f"{sum(len(v) for v in self._audit.values())} audit events, "
            f"{len(self._reports)} reports"
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

    # ── Reports ─────────────────────────────────────────────────────────

    async def create_report(
        self,
        *,
        user_email: str,
        session_id: str | None,
        title: str,
        description: str,
        tags: list[str],
        html: str,
        author_name: str = "",
    ) -> dict:
        """Create a pending report. Stores manifest.json + report.html."""
        async with self._lock:
            report_id = uuid.uuid4().hex
            now = _now_iso()
            manifest = {
                "report_id": report_id,
                "user_email": user_email.lower(),
                "author_name": author_name,
                "session_id": session_id,
                "title": title.strip()[:200],
                "description": description.strip()[:2000],
                "tags": [t.strip().lower()[:40] for t in (tags or []) if t.strip()][:12],
                "status": "pending",
                "submitted_at": now,
                "updated_at": now,
                "html_size": len(html.encode("utf-8")),
            }
            self._reports[report_id] = manifest
            await self._stage_put(
                f"reports/{report_id}/manifest.json",
                json.dumps(manifest).encode(),
            )
            await self._stage_put(
                f"reports/{report_id}/report.html",
                html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
            )
            return manifest

    async def update_report(self, report_id: str, **fields) -> dict | None:
        async with self._lock:
            manifest = self._reports.get(report_id)
            if not manifest:
                return None
            manifest.update(fields)
            manifest["updated_at"] = _now_iso()
            await self._stage_put(
                f"reports/{report_id}/manifest.json",
                json.dumps(manifest).encode(),
            )
            return manifest

    def get_report(self, report_id: str) -> dict | None:
        return self._reports.get(report_id)

    def list_reports(
        self,
        *,
        status: str | None = None,
        user_email: str | None = None,
    ) -> list[dict]:
        out = list(self._reports.values())
        if status:
            out = [r for r in out if r.get("status") == status]
        if user_email:
            email = user_email.lower()
            out = [r for r in out if r.get("user_email") == email]
        return sorted(out, key=lambda r: r.get("submitted_at", ""), reverse=True)

    async def get_report_html(self, report_id: str) -> str | None:
        if report_id not in self._reports:
            return None
        return await self._get_text(f"reports/{report_id}/report.html")

    async def delete_report(self, report_id: str) -> bool:
        """Hard-delete a report's files + drop from memory."""
        async with self._lock:
            if report_id not in self._reports:
                return False
            try:
                await self._call(self._am.edit, artifact_id=self._artifact_id, stage=True)
            except Exception:
                pass
            for path in (f"reports/{report_id}/manifest.json",
                         f"reports/{report_id}/report.html"):
                try:
                    await self._call(self._am.remove_file, artifact_id=self._artifact_id, file_path=path)
                except Exception as e:
                    logger.warning(f"delete_report remove_file({path}) failed: {e}")
            try:
                await self._call(self._am.commit, artifact_id=self._artifact_id)
            except Exception as e:
                logger.warning(f"delete_report commit failed: {e}")
            self._reports.pop(report_id, None)
            return True

    # ── LLM token accounting ───────────────────────────────────────────

    def tokens_per_user(self) -> dict[str, dict]:
        """Sum Guardian LLM tokens per user across all their sessions.

        Reads the `usage` block that the Guardian POSTs on each audit
        callback. Events without a usage block (older sessions before
        token tracking shipped) contribute 0.
        """
        by_email: dict[str, dict] = {}
        for session_id, events in self._audit.items():
            sess = self._sessions.get(session_id) or {}
            email = (sess.get("user_email") or "?").lower()
            slot = by_email.setdefault(email, {
                "user_email": email,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "guardian_calls": 0,
                "sessions": 0,
                "last_call_at": None,
            })
            for ev in events:
                u = ev.get("usage") or {}
                if not u:
                    continue
                if not (ev.get("type") or "").startswith("guardian_"):
                    continue
                slot["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
                slot["completion_tokens"] += int(u.get("completion_tokens") or 0)
                slot["total_tokens"] += int(u.get("total_tokens") or 0)
                slot["guardian_calls"] += 1
                ts = ev.get("ts") or ev.get("ts_guardian")
                if ts and (slot["last_call_at"] is None or ts > slot["last_call_at"]):
                    slot["last_call_at"] = ts
        # Sessions-per-user count (independent of audit events)
        per_user_sessions: dict[str, int] = {}
        for sess in self._sessions.values():
            e = (sess.get("user_email") or "?").lower()
            per_user_sessions[e] = per_user_sessions.get(e, 0) + 1
        for email, n in per_user_sessions.items():
            by_email.setdefault(email, {
                "user_email": email,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "guardian_calls": 0,
                "sessions": 0,
                "last_call_at": None,
            })["sessions"] = n
        return by_email

    def tokens_total(self) -> dict:
        out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "guardian_calls": 0}
        for u in self.tokens_per_user().values():
            out["prompt_tokens"] += u["prompt_tokens"]
            out["completion_tokens"] += u["completion_tokens"]
            out["total_tokens"] += u["total_tokens"]
            out["guardian_calls"] += u["guardian_calls"]
        return out

    # ── Aggregate stats ─────────────────────────────────────────────────

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
        reports = list(self._reports.values())
        tokens = self.tokens_total()
        return {
            "users_total": len(self._users),
            "users_pending": len(pending),
            "users_approved": len(approved),
            "sessions_total": len(self._sessions),
            "audit_events_total": total_calls,
            "blocks_total": total_blocks,
            "reports_total": len(reports),
            "reports_pending": sum(1 for r in reports if r.get("status") == "pending"),
            "reports_approved": sum(1 for r in reports if r.get("status") == "approved"),
            "reports_rejected": sum(1 for r in reports if r.get("status") == "rejected"),
            "tokens_total": tokens["total_tokens"],
            "tokens_prompt": tokens["prompt_tokens"],
            "tokens_completion": tokens["completion_tokens"],
            "guardian_calls": tokens["guardian_calls"],
        }
