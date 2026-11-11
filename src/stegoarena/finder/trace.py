"""Append-only attempt trace on the shared FS: every reasoning step, tool call and
result, timestamped, so the UI can replay exactly how a scheme was cracked."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..store import ArenaStore


@dataclass
class Trace:
    store: ArenaStore
    round_no: int
    attempt_id: str
    t0: float = field(default_factory=time.time)
    events: int = 0

    @property
    def base(self) -> str:
        return f"rounds/{self.round_no}/attempts/{self.attempt_id}"

    def log(self, type_: str, **data) -> None:
        self.events += 1
        self.store.append_jsonl(f"{self.base}/trace.jsonl",
                                {"i": self.events, "t": round(time.time() - self.t0, 3), "ts": time.time(), "type": type_, **data})
        if type_ == "tool_call" and data.get("tool") == "run_shell":
            self.store.append_text(f"{self.base}/commands.log", f"[{time.time() - self.t0:7.2f}s] $ {data['input'].get('command', '')}\n")

    def result(self, obj: dict) -> None:
        self.store.write_json(f"{self.base}/result.json", obj)
