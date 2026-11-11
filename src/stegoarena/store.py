"""Shared arena file system.

Keys mirror the spec layout (relative to the arena root):
    rounds/{round}/schemes/{scheme_id}/image.png | spec.json | truth.json
    rounds/{round}/attempts/{attempt_id}/trace.jsonl | commands.log | result.json
    elo.json, lineage.json

LocalStore     - a directory (default ./arena_data). Always the read path for the UI,
                 so the demo replay never depends on the network.
ArchilDiskStore- an Archil disk via its object API (put/get/append/list).
MirrorStore    - writes to both (Archil writes happen on a background thread and
                 are best-effort); reads from local.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ArenaStore(ABC):
    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None: ...
    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...
    @abstractmethod
    def append_text(self, key: str, text: str) -> None: ...
    @abstractmethod
    def list(self, prefix: str) -> list[str]: ...
    @abstractmethod
    def exists(self, key: str) -> bool: ...

    # helpers
    def write_json(self, key: str, obj: Any) -> None:
        self.write_bytes(key, json.dumps(obj, indent=2, default=str).encode())

    def read_json(self, key: str, default: Any = None) -> Any:
        if not self.exists(key):
            return default
        return json.loads(self.read_bytes(key))

    def append_jsonl(self, key: str, obj: Any) -> None:
        self.append_text(key, json.dumps(obj, default=str) + "\n")


class LocalStore(ArenaStore):
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents and p != self.root:
            raise ValueError(f"key escapes store root: {key}")
        return p

    def write_bytes(self, key: str, data: bytes) -> None:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def read_bytes(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def append_text(self, key: str, text: str) -> None:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, p.open("a") as f:
            f.write(text)

    def list(self, prefix: str) -> list[str]:
        base = self.path(prefix) if prefix else self.root
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file() and not p.name.endswith(".tmp"))

    def exists(self, key: str) -> bool:
        return self.path(key).exists()


class ArchilDiskStore(ArenaStore):
    """Archil disk object API. Keys are prefixed with `arena/`."""

    def __init__(self, disk_id: str, *, region: str | None = None, api_key: str | None = None, prefix: str = "arena/"):
        from archil import Archil

        self.client = Archil(api_key=api_key, region=region)
        self.disk = self.client.disks.get(disk_id)
        self.prefix = prefix

    def _k(self, key: str) -> str:
        return self.prefix + key

    def write_bytes(self, key: str, data: bytes) -> None:
        self.disk.put_object(self._k(key), data)

    def read_bytes(self, key: str) -> bytes:
        return self.disk.get_object(self._k(key))

    def append_text(self, key: str, text: str) -> None:
        self.disk.append_object(self._k(key), text.encode())  # <= 1 MiB per call

    def list(self, prefix: str) -> list[str]:
        res = self.disk.list_objects(self._k(prefix), recursive=True)
        objs = getattr(res, "objects", None) or getattr(res, "contents", None) or res
        return sorted(getattr(o, "key", str(o))[len(self.prefix):] for o in objs)

    def exists(self, key: str) -> bool:
        return self.disk.object_exists(self._k(key))


class MirrorStore(ArenaStore):
    def __init__(self, local: LocalStore, remote: ArenaStore):
        self.local, self.remote = local, remote
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="archil-mirror")
        self._order = threading.Lock()  # keep appends ordered per process

    def _bg(self, fn, *args) -> None:
        def run():
            try:
                with self._order:
                    fn(*args)
            except Exception as e:  # mirror is best-effort
                log.warning("archil mirror failed: %s", e)

        self._pool.submit(run)

    def write_bytes(self, key: str, data: bytes) -> None:
        self.local.write_bytes(key, data)
        self._bg(self.remote.write_bytes, key, data)

    def read_bytes(self, key: str) -> bytes:
        return self.local.read_bytes(key)

    def append_text(self, key: str, text: str) -> None:
        self.local.append_text(key, text)
        self._bg(self.remote.append_text, key, text)

    def list(self, prefix: str) -> list[str]:
        return self.local.list(prefix)

    def exists(self, key: str) -> bool:
        return self.local.exists(key)

    def flush(self) -> None:
        self._pool.shutdown(wait=True)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="archil-mirror")


def make_store(cfg) -> ArenaStore:
    local = LocalStore(cfg.arena_root)
    if cfg.archil_disk_id and cfg.archil_api_key:
        try:
            return MirrorStore(local, ArchilDiskStore(cfg.archil_disk_id, region=cfg.archil_region, api_key=cfg.archil_api_key))
        except Exception as e:
            log.warning("Archil disk unavailable (%s); using local store only", e)
    return local
