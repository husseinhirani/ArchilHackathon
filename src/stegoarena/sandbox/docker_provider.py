"""DockerProvider: template = image `stegoarena-template` (docker/Dockerfile);
fork = new container from the image, run with --network none."""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from pathlib import Path

from .base import SETUP_SCRIPT, ExecResult, Sandbox, SandboxProvider, tools_tarball

IMAGE = "stegoarena-template"


async def _run(*args: str, timeout: float = 600, stdin: bytes | None = None) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin else None,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(stdin), timeout)
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class DockerProvider(SandboxProvider):
    name = "docker"

    def __init__(self, cfg):
        self.cfg = cfg

    async def create_from_template(self) -> Sandbox:
        code, _, _ = await _run("docker", "image", "inspect", IMAGE)
        if code != 0:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "tools.tar.gz").write_bytes(tools_tarball())
                (Path(d) / "setup.sh").write_bytes(SETUP_SCRIPT.read_bytes())
                (Path(d) / "Dockerfile").write_text(
                    "FROM python:3.11-bookworm\nCOPY tools.tar.gz setup.sh /opt/arena/\n"
                    "RUN bash /opt/arena/setup.sh\nENV ARENA_TOOLS=/opt/arena PYTHONPATH=/opt/arena\nWORKDIR /work\n")
                code, out, err = await _run("docker", "build", "-t", IMAGE, d, timeout=1800)
                if code != 0:
                    raise RuntimeError(f"docker build failed: {err[-2000:]}")
        return Sandbox("template", self.name, "/work", "/opt/arena", native=IMAGE)

    async def fork(self, template: Sandbox, name: str | None = None) -> Sandbox:
        cname = name or f"stegoarena-{uuid.uuid4().hex[:8]}"
        image = template.native
        if template.id != "template":  # live container: snapshot its filesystem first
            image = f"{IMAGE}-snap:{cname}"
            await _run("docker", "commit", template.native, image)
        code, out, err = await _run("docker", "run", "-d", "--rm", "--network", "none", "--memory", "1g",
                                    "--name", cname, image, "sleep", "900")
        if code != 0:
            raise RuntimeError(err)
        return Sandbox(cname, self.name, "/work", "/opt/arena", native=cname)

    async def exec(self, sb: Sandbox, command: str, timeout_s: float = 20) -> ExecResult:
        t0 = time.monotonic()
        try:
            code, out, err = await _run("docker", "exec", "-w", sb.workdir, sb.native, "bash", "-c", command, timeout=timeout_s)
            return ExecResult(code, out, err, time.monotonic() - t0)
        except asyncio.TimeoutError:
            await _run("docker", "exec", sb.native, "pkill", "-f", "-9", ".")
            return ExecResult(124, "", "timed out", time.monotonic() - t0, True)

    async def read_file(self, sb: Sandbox, path: str) -> bytes:
        p = path if path.startswith("/") else f"{sb.workdir}/{path}"
        p_ = await asyncio.create_subprocess_exec("docker", "exec", sb.native, "cat", p, stdout=asyncio.subprocess.PIPE)
        out, _ = await p_.communicate()
        return out

    async def write_file(self, sb: Sandbox, path: str, data: bytes) -> None:
        p = path if path.startswith("/") else f"{sb.workdir}/{path}"
        await _run("docker", "exec", "-i", sb.native, "bash", "-c", f"mkdir -p $(dirname {p}) && cat > {p}", stdin=data)

    async def destroy(self, sb: Sandbox) -> None:
        await _run("docker", "rm", "-f", sb.native)
