"""Per-session kernel pool with lifecycle management.

One IPython kernel per portal session. Kernels run inside the same pod
(weak isolation — separate Python processes share the host but each has
its own namespace).

Three reasons a kernel/session can be reaped:

1. **Idle timeout** — no `run_code` call within `idle_timeout_sec`.
   Default 30 minutes. Tunable per-deployment.

2. **Max lifetime** — kernel has been alive for more than
   `max_lifetime_sec` regardless of activity. Default 24 hours.
   Prevents long-lived kernels from accumulating memory or holding
   resources indefinitely.

3. **Pool saturation** — if a new session needs a slot and the pool is
   at capacity, the longest-idle kernel is evicted before the new one
   starts.

The reaper runs every `reaper_interval_sec` (default 5 min). On reap:
- Stops the kernel cleanly.
- Calls `on_session_ended(session_id, reason)` so the caller (the app)
  can mark the session `ended` in the store and append an audit event.
  The pool itself does not touch the store.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from .kernel import PortalKernel

logger = logging.getLogger("portal.kernel_pool")

EndedCallback = Callable[[str, str], Awaitable[None]]


class KernelPool:
    def __init__(
        self,
        max_kernels: int = 32,
        idle_timeout_sec: int = 30 * 60,
        max_lifetime_sec: int = 24 * 3600,
        reaper_interval_sec: int = 5 * 60,
        data_dir: str = "/data",
        work_dir: str = "/work",
        env: dict | None = None,
        on_session_ended: EndedCallback | None = None,
    ):
        self.max_kernels = max_kernels
        self.idle_timeout_sec = idle_timeout_sec
        self.max_lifetime_sec = max_lifetime_sec
        self.reaper_interval_sec = reaper_interval_sec
        self.data_dir = data_dir
        self.work_dir = work_dir
        self.env = env or {}
        self.on_session_ended = on_session_ended
        self._kernels: dict[str, PortalKernel] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> PortalKernel:
        """Return the kernel for this session, starting it on demand."""
        async with self._lock:
            kernel = self._kernels.get(session_id)
            if kernel is None:
                if len(self._kernels) >= self.max_kernels:
                    await self._evict_one(reason="pool_capacity")
                if len(self._kernels) >= self.max_kernels:
                    raise RuntimeError(
                        f"Kernel pool at capacity ({self.max_kernels})."
                    )
                kernel = PortalKernel(cwd=self.work_dir, env=self.env)
                await kernel.start()
                self._kernels[session_id] = kernel
                logger.info(
                    "Started kernel for session %s (%d/%d)",
                    session_id, len(self._kernels), self.max_kernels,
                )
            kernel.last_used_at = time.time()
            return kernel

    async def stop(self, session_id: str, reason: str = "user_request") -> bool:
        """Stop the kernel for one session, if any. Returns True if stopped."""
        async with self._lock:
            kernel = self._kernels.pop(session_id, None)
        if kernel is None:
            return False
        try:
            await kernel.stop()
            logger.info("Stopped kernel for session %s (reason=%s)", session_id, reason)
        except Exception as e:
            logger.warning("Stop error for %s: %s", session_id, e)
        if self.on_session_ended:
            try:
                await self.on_session_ended(session_id, reason)
            except Exception as e:
                logger.warning("on_session_ended callback failed: %s", e)
        return True

    async def reaper_loop(self):
        """Run forever, evicting kernels that hit idle or lifetime limits."""
        while True:
            try:
                await asyncio.sleep(self.reaper_interval_sec)
                await self._reap()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Reaper iteration failed: %s", e)

    async def _reap(self):
        now = time.time()
        async with self._lock:
            doomed: list[tuple[str, PortalKernel, str]] = []
            for sid, k in self._kernels.items():
                age = now - k.started_at if k.started_at else 0
                idle = now - k.last_used_at if k.last_used_at else 0
                if age > self.max_lifetime_sec:
                    doomed.append((sid, k, "max_lifetime"))
                elif idle > self.idle_timeout_sec:
                    doomed.append((sid, k, "idle_timeout"))
            for sid, _, _ in doomed:
                self._kernels.pop(sid, None)
        for sid, k, reason in doomed:
            try:
                await k.stop()
            except Exception as e:
                logger.warning("Reaper stop error for %s: %s", sid, e)
            logger.info("Reaped session %s (%s)", sid, reason)
            if self.on_session_ended:
                try:
                    await self.on_session_ended(sid, reason)
                except Exception as e:
                    logger.warning("on_session_ended callback failed for %s: %s", sid, e)

    async def _evict_one(self, reason: str = "pool_capacity"):
        """Evict the longest-idle kernel; caller must hold the lock."""
        if not self._kernels:
            return
        sid_oldest = min(self._kernels, key=lambda s: self._kernels[s].last_used_at)
        kernel = self._kernels.pop(sid_oldest)
        try:
            await kernel.stop()
        except Exception:
            pass
        logger.info("Evicted session %s (%s)", sid_oldest, reason)
        if self.on_session_ended:
            try:
                await self.on_session_ended(sid_oldest, reason)
            except Exception as e:
                logger.warning("on_session_ended callback failed: %s", e)

    async def shutdown(self):
        async with self._lock:
            sessions = list(self._kernels.items())
            self._kernels.clear()
        for sid, k in sessions:
            try:
                await k.stop()
            except Exception:
                pass

    def stats(self) -> dict[str, Any]:
        now = time.time()
        sessions = []
        for sid, k in self._kernels.items():
            sessions.append({
                "session_id": sid,
                "age_sec": int(now - k.started_at) if k.started_at else 0,
                "idle_sec": int(now - k.last_used_at) if k.last_used_at else 0,
                "exec_count": k.exec_count,
            })
        return {
            "active_kernels": len(self._kernels),
            "max_kernels": self.max_kernels,
            "idle_timeout_sec": self.idle_timeout_sec,
            "max_lifetime_sec": self.max_lifetime_sec,
            "sessions": sessions,
        }
