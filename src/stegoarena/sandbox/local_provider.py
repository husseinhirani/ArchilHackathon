"""LocalProvider: subprocess in a per-attempt temp dir. No isolation - dev/test only.
Uses whatever stego CLIs exist on the host (brew install exiftool binwalk ...)."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from .base import ExecResult, Sandbox, SandboxProvider, copy_tools


class LocalProvider(SandboxProvider):
    name = "local"

    def __init__(self, cfg):
        self.root = Path(cfg.cache_dir) / "local_sandboxes"
        self.root.mkdir(parents=True, exist_ok=True)

    async def create_from_template(self) -> Sandbox:
        tdir = self.root / "template"
        copy_tools(tdir / "tools")
        (tdir / "work").mkdir(exist_ok=True)
        return Sandbox("template", self.name, str(tdir / "work"), str(tdir / "tools"), native=tdir)

    async def fork(self, template: Sandbox, name: str | None = None) -> Sandbox:
        sid = name or f"fork-{uuid.uuid4().hex[:8]}"
        d = self.root / sid
        await asyncio.to_thread(shutil.copytree, template.native, d, dirs_exist_ok=True)
        return Sandbox(sid, self.name, str(d / "work"), str(d / "tools"), native=d)

    async def exec(self, sb: Sandbox, command: str, timeout_s: float = 20) -> ExecResult:
        env = dict(os.environ)
        env.update(ARENA_TOOLS=sb.tools_dir, PYTHONPATH=sb.tools_dir,
                   PATH=f"{Path(sys.executable).parent}:{env.get('PATH', '')}")
        for k in [k for k in env if k.endswith("_API_KEY")]:
            env.pop(k)  # never leak host secrets into the "sandbox"
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=sb.workdir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True
        return ExecResult(proc.returncode if not timed_out else 124, out.decode(errors="replace"),
                          err.decode(errors="replace"), time.monotonic() - t0, timed_out)

    def _p(self, sb: Sandbox, path: str) -> Path:
        return Path(path) if os.path.isabs(path) else Path(sb.workdir) / path

    async def read_file(self, sb: Sandbox, path: str) -> bytes:
        return self._p(sb, path).read_bytes()

    async def write_file(self, sb: Sandbox, path: str, data: bytes) -> None:
        p = self._p(sb, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    async def destroy(self, sb: Sandbox) -> None:
        if sb.id != "template":
            await asyncio.to_thread(shutil.rmtree, sb.native, True)
