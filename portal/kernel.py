"""Minimal IPython-kernel wrapper.

This is a small, self-contained replacement for the kernel manager from
safe-colab-cli. We only ever run kernels with no in-process sandbox (the
pod itself is the trust boundary), so we don't need any of the
nono/bubblewrap/Docker provisioners. One process per session, one
Jupyter kernel each, IO collected from iopub messages.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from jupyter_client import KernelManager

logger = logging.getLogger("portal.kernel")


class PortalKernel:
    """One IPython kernel; persistent state across `execute()` calls."""

    def __init__(self, cwd: str | None = None, env: dict | None = None):
        self._km: KernelManager | None = None
        self._kc = None
        self._cwd = cwd
        self._env = env or {}
        self.started_at: float = 0.0
        self.last_used_at: float = 0.0
        self.exec_count: int = 0

    async def start(self):
        """Start the kernel subprocess. Idempotent: re-start = no-op."""
        if self._km is not None:
            return
        self._km = KernelManager(kernel_name="python3")
        if self._cwd:
            try:
                os.makedirs(self._cwd, exist_ok=True)
            except Exception:
                pass
        if self._env:
            self._km.extra_env = self._env
        kwargs: dict[str, Any] = {}
        if self._cwd:
            kwargs["cwd"] = self._cwd
        # start_kernel blocks until the kernel process is spawned;
        # offload to default executor so we don't stall the loop.
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._km.start_kernel(**kwargs)
        )
        self._kc = self._km.client()
        self._kc.start_channels()
        await asyncio.get_event_loop().run_in_executor(
            None, self._kc.wait_for_ready, 30
        )
        self.started_at = time.time()
        self.last_used_at = self.started_at
        logger.info("Kernel started (cwd=%s)", self._cwd)

    async def execute(self, code: str, timeout: float = 120) -> dict:
        """Execute code and collect stdout / stderr / result / error."""
        if self._kc is None:
            raise RuntimeError("Kernel not started — call start() first")
        self.last_used_at = time.time()
        self.exec_count += 1
        msg_id = self._kc.execute(code)
        return await asyncio.get_event_loop().run_in_executor(
            None, self._collect_output, msg_id, timeout
        )

    def _collect_output(self, msg_id: str, timeout: float) -> dict:
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        result: str | None = None
        error: dict | None = None
        display_data: list[dict] = []

        while True:
            try:
                msg = self._kc.get_iopub_msg(timeout=timeout)
            except Exception:
                break
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            mtype = msg["msg_type"]
            content = msg["content"]
            if mtype == "stream":
                target = stdout_parts if content["name"] == "stdout" else stderr_parts
                target.append(content["text"])
            elif mtype == "execute_result":
                result = content["data"].get("text/plain", "")
            elif mtype == "display_data":
                display_data.append(content["data"])
            elif mtype == "error":
                error = {
                    "ename": content["ename"],
                    "evalue": content["evalue"],
                    "traceback": content["traceback"],
                }
            elif mtype == "status" and content["execution_state"] == "idle":
                break

        return {
            "stdout": "".join(stdout_parts),
            "stderr": "".join(stderr_parts),
            "result": result,
            "error": error,
            "display_data": display_data,
        }

    async def stop(self):
        if self._kc:
            try:
                self._kc.stop_channels()
            except Exception:
                pass
        if self._km:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._km.shutdown_kernel(now=True)
                )
            except Exception as e:
                logger.warning("Kernel shutdown error: %s", e)
        self._km = None
        self._kc = None
        logger.info("Kernel stopped")
