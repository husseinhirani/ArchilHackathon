"""LLM finder: Claude tool loop, host-side; every shell command runs in the Archil fork.

Why host-side: Archil Free-tier egress is allowlisted, and keeping API keys out of
the sandbox is safer anyway. Only `run_shell` touches the sandbox.

Manual loop (not the beta tool runner) because we need a hard wall-clock budget,
per-step tracing, and to stop the moment `submit_flag` is called.
"""

from __future__ import annotations

import asyncio
import json
import time

import anthropic

from ..sandbox.base import Sandbox, SandboxProvider
from .search import SearchProvider
from .trace import Trace

VERSION = "claude-finder-v1"

SYSTEM = """You are a steganalysis agent in a timed CTF. One PNG in your working directory, `challenge.png`, hides a flag of the form FLAG{{...}}. You have {budget:.0f} seconds of wall-clock time in total; every tool call costs time. Be fast and decisive.

Environment (Linux sandbox, cwd contains challenge.png):
- CLI tools that may be present: zsteg, binwalk, exiftool, steghide, stegseek, pngcheck, strings, xxd, file, python3 (numpy, Pillow, cryptography).
- Kerckhoffs's principle: the hider's algorithm library is importable as `arena_schemes` (source in $ARENA_TOOLS/arena_schemes; read api.py, spec.py, techniques.py, bits.py if needed). You know the algorithms, not the parameters or the key.
  * `from arena_schemes import extract, load_png, SchemeSpec` then `extract(load_png(open('challenge.png','rb').read()), SchemeSpec(technique=..., ...), key)` returns the flag or None.
  * Spec fields: technique lsb|dct|palette|alpha; channels; bit_plane 0..2; order sequential|prng (prng is seeded from the key); dct_coeff "u,v"; dct_delta 12..32 (even values are typical); palette_colors; alpha_mode alpha_lsb|transparent_rgb; cipher none|xor|aes; key_source none|metadata|wordlist|secret.
  * Keys may sit in PNG text metadata, or be a word from $ARENA_TOOLS/arena_schemes/wordlist.txt, or be unrecoverable.
  * `python3 -m arena_schemes.triage challenge.png` runs a broad automated sweep (options: --quick, --keys k1,k2, --wordlist, --budget SECONDS). Always pass a --budget that fits your remaining time.
- Hiders plant DECOY flags (e.g. in metadata text or appended after IEND). A flag recovered by actually extracting the payload with a scheme is far more trustworthy than one sitting in plain bytes.

Scoring: partial credit. Call report_signal as soon as you have evidence (anomaly seen, technique family, params). Submitting a decoy is penalised. Call submit_flag exactly once, when confident or when time is nearly out. If you cannot recover the flag, still report your best technique/params guess.

Use search_web / fetch_page only when your first triage finds nothing and you need a technique you don't know."""

TOOLS = [
    {"name": "run_shell", "description": "Run a bash command in the sandbox (cwd holds challenge.png). Output is truncated. Default timeout 20s, max 45s.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_s": {"type": "integer", "minimum": 1, "maximum": 45}},
                      "required": ["command"], "additionalProperties": False}},
    {"name": "search_web", "description": "Web search (Keenable) for steganalysis techniques or tool usage. Returns titles, urls, snippets.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
    {"name": "fetch_page", "description": "Fetch a web page (from search results) as markdown.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}},
    {"name": "report_signal", "description": "Record evidence for partial credit: what anomaly you detected, which technique family, and any params you believe.",
     "input_schema": {"type": "object", "properties": {
         "anomaly": {"type": "string"},
         "technique": {"type": "string", "enum": ["lsb", "dct", "palette", "alpha", "unknown"]},
         "params": {"type": "object", "description": "e.g. {\"channels\":[\"R\"],\"bit_plane\":0,\"order\":\"prng\",\"cipher\":\"xor\"}"}},
         "required": ["anomaly", "technique"], "additionalProperties": False}},
    {"name": "submit_flag", "description": "Submit your final answer. Ends the attempt.",
     "input_schema": {"type": "object", "properties": {"flag": {"type": "string"}, "confidence": {"type": "number"}},
                      "required": ["flag"], "additionalProperties": False}},
]


class ClaudeFinder:
    def __init__(self, cfg, search: SearchProvider):
        self.cfg = cfg
        self.search = search
        self.client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key, max_retries=2)

    async def run(self, provider: SandboxProvider, sb: Sandbox, trace: Trace) -> dict:
        budget = self.cfg.finder_budget_s
        t0 = time.monotonic()
        remaining = lambda: budget - (time.monotonic() - t0)  # noqa: E731
        messages: list = [{"role": "user", "content": "Begin. The image is ./challenge.png."}]
        signals: list[dict] = []
        submitted: str | None = None
        stop_reason, turns = "budget", 0
        trace.log("start", finder=VERSION, model=self.cfg.finder_model, budget_s=budget, sandbox=sb.id, provider=sb.provider)

        while remaining() > 3 and turns < self.cfg.finder_max_turns and submitted is None:
            turns += 1
            try:
                resp = await asyncio.wait_for(self._call(messages, budget), timeout=max(5.0, remaining()))
            except (asyncio.TimeoutError, anthropic.APITimeoutError):
                stop_reason = "budget"
                trace.log("error", message="model call exceeded remaining budget")
                break
            except anthropic.APIError as e:
                stop_reason = "api_error"
                trace.log("error", message=f"{type(e).__name__}: {e}")
                break

            for b in resp.content:
                if b.type == "thinking" and getattr(b, "thinking", ""):
                    trace.log("reasoning", text=b.thinking)
                elif b.type == "text" and b.text.strip():
                    trace.log("text", text=b.text)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                stop_reason = "refusal"
                trace.log("error", message="model refused")
                break
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                stop_reason = "end_turn"
                break

            async def handle(tu):
                trace.log("tool_call", tool=tu.name, input=tu.input, id=tu.id)
                out, is_err = await self._exec_tool(tu.name, tu.input, provider, sb, remaining, signals, trace)
                return {"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": is_err}

            results = await asyncio.gather(*(handle(tu) for tu in tool_uses))  # all results in ONE user message
            content = list(results)
            if remaining() < 20:  # nudge after the tool results, same user turn
                content.append({"type": "text", "text": f"[{remaining():.0f}s left: report_signal your best guess and submit_flag now.]"})
            messages.append({"role": "user", "content": content})
            for tu in tool_uses:
                if tu.name == "submit_flag":
                    submitted = str(tu.input.get("flag", "")).strip()
                    stop_reason = "submitted"

        result = {"finder": VERSION, "model": self.cfg.finder_model, "submitted_flag": submitted, "signals": signals,
                  "turns": turns, "elapsed_s": round(time.monotonic() - t0, 2), "stop_reason": stop_reason}
        trace.log("end", **{k: v for k, v in result.items() if k != "signals"})
        return result

    async def _call(self, messages: list, budget: float):
        return await self.client.beta.messages.create(
            model=self.cfg.finder_model,
            max_tokens=8000,
            system=SYSTEM.format(budget=budget),
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},  # summaries feed the replay view
            output_config={"effort": self.cfg.finder_effort},
            cache_control={"type": "ephemeral"},  # K parallel forks share the system+tools prefix
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    async def _exec_tool(self, name, inp, provider, sb, remaining, signals, trace) -> tuple[str, bool]:
        try:
            if name == "run_shell":
                timeout = min(float(inp.get("timeout_s", 20)), 45.0, max(2.0, remaining() - 2))
                res = await provider.exec(sb, inp["command"], timeout_s=timeout)
                out = res.for_model()
                trace.log("tool_result", tool=name, output=out, duration_s=round(res.duration_s, 2), exit_code=res.exit_code)
                return out + f"\n[{remaining():.0f}s left]", res.exit_code != 0
            if name == "search_web":
                hits = await self.search.search(inp["query"], max_results=5)
                trace.log("search", provider=self.search.name, query=inp["query"], titles=[h["title"] for h in hits])
                return json.dumps(hits)[:6000], False
            if name == "fetch_page":
                page = await self.search.fetch(inp["url"])
                trace.log("fetch", provider=self.search.name, url=inp["url"], title=page.get("title"))
                return json.dumps(page)[:6000], False
            if name == "report_signal":
                sig = {"anomaly": inp.get("anomaly", ""), "technique": inp.get("technique"), "params": inp.get("params") or {}}
                signals.append(sig)
                trace.log("signal", **sig)
                return "recorded", False
            if name == "submit_flag":
                trace.log("submit", flag=inp.get("flag"), confidence=inp.get("confidence"))
                return "submitted", False
            return f"unknown tool {name}", True
        except Exception as e:
            trace.log("tool_result", tool=name, output=f"error: {e}", error=True)
            return f"error: {type(e).__name__}: {e}", True
