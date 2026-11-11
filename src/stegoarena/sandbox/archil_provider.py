"""ArchilProvider: one tool-loaded template sandbox, a fork per finder attempt.

Verified against archil==0.21.0 (Python SDK; every method has an `.aio` coroutine):
    client.sandboxes.create(name=, base_image=, max_ttl_seconds=, idle_ttl_seconds=, ...)
    client.sandboxes.get(id)
    sandbox.exec(command, cwd=, env=, timeout_seconds=) -> SandboxProcessResult(status, stdout, stderr, exit_code)
    sandbox.fork(name=, wait=)  (stopped source -> child boots from disk)
    sandbox.start/stop/delete, sandbox.files.upload_file(local, remote)/download_file(remote, local)
Notes from docs: a sandbox with mounted disks must be stopped before fork, so the
template has NO mounts; the shared FS is written host-side via ArchilDiskStore.
Free tier: 5 concurrent sandboxes, 30-min sessions, allowlisted egress - which is
why the finder's LLM loop runs host-side and only shell commands run in forks.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
import uuid
from pathlib import Path

from .base import SETUP_SCRIPT, ExecResult, Sandbox, SandboxProvider, tools_tarball

log = logging.getLogger(__name__)
TEMPLATE_NAME = "stegoarena-template"
ENV = {"ARENA_TOOLS": "/opt/arena", "PYTHONPATH": "/opt/arena"}


class ArchilProvider(SandboxProvider):
    name = "archil"

    def __init__(self, cfg):
        from archil import Archil

        if not cfg.archil_api_key:
            raise RuntimeError("ARCHIL_API_KEY not set - use STEGOARENA_PROVIDER=local or docker")
        self.cfg = cfg
        self.client = Archil(api_key=cfg.archil_api_key, region=cfg.archil_region, timeout=120)
        self.cache = Path(cfg.cache_dir) / "archil_template.json"

    def _wrap(self, native, sid: str | None = None) -> Sandbox:
        return Sandbox(sid or native.id, self.name, "/work", "/opt/arena", native=native)

    async def create_from_template(self) -> Sandbox:
        if self.cache.exists():
            tid = json.loads(self.cache.read_text())["id"]
            try:
                sb = await self.client.sandboxes.get.aio(tid)
                if sb.status not in ("failed", "deleted"):
                    return self._wrap(sb)
            except Exception as e:
                log.warning("cached template %s unusable (%s); rebuilding", tid, e)
        return await self.build_template()

    async def build_template(self) -> Sandbox:
        t0 = time.monotonic()
        sb = await self.client.sandboxes.create.aio(
            name=f"{TEMPLATE_NAME}-{uuid.uuid4().hex[:6]}", base_image=self.cfg.archil_base_image,
            max_ttl_seconds=1800, idle_ttl_seconds=600)
        wrapped = Sandbox(sb.id, self.name, "/", "/opt/arena", native=sb)  # /work doesn't exist yet
        await self.exec(wrapped, "mkdir -p /opt/arena /work", 30)
        await self.write_file(wrapped, "/opt/arena/tools.tar.gz", tools_tarball())
        await self.write_file(wrapped, "/opt/arena/setup.sh", SETUP_SCRIPT.read_bytes())
        res = await self.exec(wrapped, "bash /opt/arena/setup.sh", timeout_s=1200)
        log.info("template setup exit=%s\n%s", res.exit_code, res.stdout[-2000:])
        if res.exit_code != 0:
            raise RuntimeError(f"template setup failed: {res.stderr[-2000:]}")
        await sb.stop.aio(wait=True)  # stopped template: forks boot from its disk
        self.cache.write_text(json.dumps({"id": sb.id, "built_in_s": round(time.monotonic() - t0, 1)}))
        return self._wrap(sb)

    async def fork(self, template: Sandbox, name: str | None = None) -> Sandbox:
        child = await template.native.fork.aio(name=name or f"finder-{uuid.uuid4().hex[:8]}", wait=True)
        if child.status != "running":
            child = await child.start.aio(wait=True)
        return self._wrap(child)

    async def exec(self, sb: Sandbox, command: str, timeout_s: float = 20) -> ExecResult:
        t0 = time.monotonic()
        r = await sb.native.exec.aio(command, cwd=sb.workdir,
                                     env=ENV, timeout_seconds=max(1, int(timeout_s)))
        timed_out = getattr(r, "status", "") == "timed_out"
        code = r.exit_code if r.exit_code is not None else (124 if timed_out else 1)
        return ExecResult(code, r.stdout or "", r.stderr or "", time.monotonic() - t0, timed_out)

    async def read_file(self, sb: Sandbox, path: str) -> bytes:
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "f"
            await sb.native.files.download_file.aio(self._abs(sb, path), str(local))
            return local.read_bytes()

    async def write_file(self, sb: Sandbox, path: str, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "f"
            local.write_bytes(data)
            await sb.native.files.upload_file.aio(str(local), self._abs(sb, path))

    @staticmethod
    def _abs(sb: Sandbox, path: str) -> str:
        return path if path.startswith("/") else f"{sb.workdir}/{path}"

    async def destroy(self, sb: Sandbox) -> None:
        try:
            await sb.native.stop.aio(wait=True)
            await sb.native.delete.aio()
        except Exception as e:
            log.warning("destroy %s failed: %s", sb.id, e)
