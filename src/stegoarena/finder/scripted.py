"""finder-v0: non-LLM baseline. Runs quick triage in the sandbox and submits the
first flag it sees. Fooled by decoys, blind to keys - which is the point. Also the
smoke test for sandbox + tooling: it must crack the known-easy scheme 100%."""

from __future__ import annotations

import json
import time

from ..sandbox.base import Sandbox, SandboxProvider
from .trace import Trace

VERSION = "scripted-v0"


async def run_scripted(provider: SandboxProvider, sb: Sandbox, trace: Trace, image_name: str = "challenge.png") -> dict:
    t0 = time.monotonic()
    cmd = f"python3 -m arena_schemes.triage {image_name} --quick"
    trace.log("tool_call", tool="run_shell", input={"command": cmd})
    res = await provider.exec(sb, cmd, timeout_s=60)
    trace.log("tool_result", tool="run_shell", output=res.for_model(), duration_s=round(res.duration_s, 2))
    submitted, signals = None, []
    try:
        out = json.loads(res.stdout)
        if out["flags"]:
            first = out["flags"][0]
            submitted, where, spec = first["flag"], first["where"], first.get("spec")
            if spec:
                signals.append({"anomaly": f"payload recovered via {spec['technique']}",
                                "technique": spec["technique"], "params": spec})
            trace.log("submit", flag=submitted, where=where)
    except Exception as e:
        trace.log("error", message=f"could not parse triage output: {e}")
    return {"finder": VERSION, "submitted_flag": submitted, "signals": signals, "turns": 1,
            "elapsed_s": round(time.monotonic() - t0, 2), "stop_reason": "scripted"}
