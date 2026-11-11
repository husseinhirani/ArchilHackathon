"""SandboxProvider interface. Finder tool calls only ever touch a Sandbox through this."""

from __future__ import annotations

import io
import shutil
import tarfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = Path(__file__).resolve().parent / "setup_template.sh"
MAX_OUTPUT = 6000  # chars of stdout/stderr returned to the model per command


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    def for_model(self) -> str:
        def clip(s: str) -> str:
            return s if len(s) <= MAX_OUTPUT else s[: MAX_OUTPUT // 2] + f"\n...[{len(s) - MAX_OUTPUT} chars clipped]...\n" + s[-MAX_OUTPUT // 2 :]

        parts = [f"exit_code={self.exit_code}" + (" (TIMED OUT)" if self.timed_out else "")]
        if self.stdout:
            parts.append("stdout:\n" + clip(self.stdout))
        if self.stderr:
            parts.append("stderr:\n" + clip(self.stderr))
        return "\n".join(parts)


@dataclass
class Sandbox:
    id: str
    provider: str
    workdir: str  # where the challenge lives (cwd for commands)
    tools_dir: str  # $ARENA_TOOLS: arena_schemes package + wordlist
    native: object = None  # provider-specific handle
    meta: dict = field(default_factory=dict)


class SandboxProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def create_from_template(self) -> Sandbox:
        """Return the (cached) tool-loaded template sandbox, building it if needed."""

    @abstractmethod
    async def fork(self, template: Sandbox, name: str | None = None) -> Sandbox:
        """Cheap copy of the template for one finder attempt."""

    @abstractmethod
    async def exec(self, sb: Sandbox, command: str, timeout_s: float = 20) -> ExecResult: ...

    @abstractmethod
    async def read_file(self, sb: Sandbox, path: str) -> bytes: ...

    @abstractmethod
    async def write_file(self, sb: Sandbox, path: str, data: bytes) -> None: ...

    @abstractmethod
    async def destroy(self, sb: Sandbox) -> None: ...

    async def close(self) -> None:
        pass


def tools_tarball() -> bytes:
    """arena_schemes package (Kerckhoffs: algorithms are public) as a .tar.gz."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(PKG_DIR / "arena_schemes", arcname="arena_schemes",
                filter=lambda ti: None if "__pycache__" in ti.name else ti)
    return buf.getvalue()


def copy_tools(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PKG_DIR / "arena_schemes", dest / "arena_schemes", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))


def make_provider(cfg) -> SandboxProvider:
    name = cfg.provider
    if name == "archil":
        from .archil_provider import ArchilProvider

        return ArchilProvider(cfg)
    if name == "docker":
        from .docker_provider import DockerProvider

        return DockerProvider(cfg)
    from .local_provider import LocalProvider

    return LocalProvider(cfg)
