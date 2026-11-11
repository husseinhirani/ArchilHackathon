"""LLM finder: a Claude tool loop running host-side; every shell command runs in an Archil fork.

What makes one finder different from another is its GENOME, never the model:
  * persona      - strategy prompt fragment
  * playbook     - lessons it wrote for itself after earlier rounds (finder/learn.py)
  * toolkit      - scripts it wrote with save_tool, validated and shipped into every fork
  * branching    - `branch` forks the live sandbox AND the conversation into parallel
                   hypotheses; the first branch to extract the flag wins.

Manual loop (not the beta tool runner) for the hard wall-clock budget, per-step tracing,
mid-loop forking, and stopping the moment a flag is submitted.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field

import anthropic

from ..genome import TOOL_NAME_RE, FinderGenome, static_finder
from ..sandbox.base import Sandbox, SandboxProvider
from .search import SearchProvider
from .trace import Trace

BASE_SYSTEM = """You are a steganalysis agent in a timed CTF. One PNG in your working directory, `challenge.png`, hides a flag of the form FLAG{{...}}. You have {budget:.0f} seconds of wall-clock time in total; every tool call costs time.

Environment (Linux sandbox, cwd contains challenge.png):
- CLI tools: zsteg, binwalk, exiftool, steghide, stegseek, pngcheck, strings, xxd, file, python3 (numpy, Pillow, cryptography).
- Kerckhoffs's principle: the hider's algorithm library is importable as `arena_schemes` (source in $ARENA_TOOLS/arena_schemes: api.py, spec.py, techniques.py, bits.py, bruteforce.py). You know the algorithms, not the parameters or the key.
  * `from arena_schemes import extract, load_png, SchemeSpec`; `extract(load_png(open('challenge.png','rb').read()), SchemeSpec(technique=..., ...), key)` returns the flag or None.
  * Spec fields: technique lsb|dct|palette|alpha; channels; bit_plane 0..2; order sequential|prng (prng seeded by the key); dct_coeff "u,v" (QIM on one 8x8 block-DCT coefficient of channels[0]); dct_delta 12..32 (even); palette_colors; alpha_mode alpha_lsb|transparent_rgb; cipher none|xor|aes.
  * Keys: in PNG text metadata, or a word from arena_schemes/wordlist.txt (~160), or a BIGLIST key = word + two digits 00..99 (~16k). Every scheme is solvable.
  * `python3 -m arena_schemes.triage challenge.png [--wordlist] [--biglist] [--only lsb,dct] [--channels G] [--bit-planes 0] [--deltas 16,18] [--coeffs "2,1;3,2"] [--keys k1,k2] [--budget S]` searches carriers x orders x ciphers x keys. Metadata keys + wordlist take seconds. The biglist across ALL carriers takes minutes - narrow first (one dct channel+delta: ~1s; one lsb channel+bit-plane: ~40s). JSON output marks payloads it EXTRACTED vs flags merely seen in plain bytes.
- Hiders plant DECOY flags in plain bytes (metadata text, data after IEND). Never submit a flag only seen in plain bytes.
- Escalate rather than give up while time remains. If time runs out without an extracted flag, report_signal your best technique/params guess and submit an empty string.

Unfamiliar signals: when a tool shows something you can't explain (an unusual PNG chunk, a statistical anomaly, a tool error or flag you don't know, an encoding you don't recognise) and your playbook doesn't cover it, spend ONE search_web call on it before guessing, then act on what you learn. The harness will point these out.

Scoring: partial credit for report_signal evidence (anomaly, technique, params); decoys are penalised. Call submit_flag once: as soon as you have an EXTRACTED flag, or when under 10s remain."""

LEARNING_SYSTEM = """
{persona}

== Your playbook (lessons YOU wrote after earlier rounds; trust but verify) ==
{playbook}

== Your toolkit: scripts YOU wrote earlier, preinstalled at $ARENA_TOOLS/mytools/ ==
{toolkit}
Run with: python3 $ARENA_TOOLS/mytools/<name>.py challenge.png

== Growing your toolkit ==
When you write analysis code that would help on future images (a detector, a narrowed search, a parser), call save_tool so future you starts with it. Tools must run as `python3 tool.py <image.png>` and print a short result.

== Branching ==
You may call `branch` at most once with 2-{max_branches} competing hypotheses (e.g. "DCT on G, biglist narrowed" vs "LSB R bit-plane 1"). Your live sandbox (files, computed state) and this conversation are forked once per hypothesis and run in parallel; the first branch to EXTRACT the flag wins and you get every branch's findings back. Forking a live sandbox costs ~10s, so branch only when you have genuinely different leads, each needing real compute (e.g. two narrowed biglist searches), and >35s left. Don't branch for a single obvious lead."""

TOOL_DEFS = {
    "run_shell": {"name": "run_shell", "description": "Run a bash command in the sandbox (cwd holds challenge.png). Output is truncated. Default timeout 20s, max 60s.",
                  "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_s": {"type": "integer", "minimum": 1, "maximum": 60}},
                                   "required": ["command"], "additionalProperties": False}},
    "search_web": {"name": "search_web", "description": "Web search (Keenable) for steganalysis techniques or tool usage. Returns titles, urls, snippets.",
                   "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
    "fetch_page": {"name": "fetch_page", "description": "Fetch a web page (from search results) as markdown.",
                   "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}},
    "report_signal": {"name": "report_signal", "description": "Record evidence for partial credit: the anomaly you detected, the technique family, and any params you believe.",
                      "input_schema": {"type": "object", "properties": {
                          "anomaly": {"type": "string"},
                          "technique": {"type": "string", "enum": ["lsb", "dct", "palette", "alpha", "unknown"]},
                          "params": {"type": "object", "description": "e.g. {\"channels\":[\"R\"],\"bit_plane\":0,\"order\":\"prng\",\"cipher\":\"xor\"}"}},
                          "required": ["anomaly", "technique"], "additionalProperties": False}},
    "submit_flag": {"name": "submit_flag", "description": "Submit your final answer. Ends the attempt.",
                    "input_schema": {"type": "object", "properties": {"flag": {"type": "string"}, "confidence": {"type": "number"}},
                                     "required": ["flag"], "additionalProperties": False}},
    "save_tool": {"name": "save_tool", "description": "Save a reusable Python script into your persistent toolkit (also usable immediately at $ARENA_TOOLS/mytools/<name>.py). It must run as `python3 <name>.py <image.png>`.",
                  "input_schema": {"type": "object", "properties": {
                      "name": {"type": "string", "description": "snake_case, 3-40 chars"},
                      "description": {"type": "string", "description": "one line: what it detects/does and when to use it"},
                      "code": {"type": "string"}}, "required": ["name", "description", "code"], "additionalProperties": False}},
    "branch": {"name": "branch", "description": "Fork your live sandbox and this conversation into 2-3 parallel hypotheses. At most once per attempt. Returns every branch's findings; the first branch to extract the flag stops the others.",
               "input_schema": {"type": "object", "properties": {"hypotheses": {"type": "array", "minItems": 2, "maxItems": 3, "items": {
                   "type": "object", "properties": {"label": {"type": "string"}, "plan": {"type": "string"}},
                   "required": ["label", "plan"], "additionalProperties": False}}},
                   "required": ["hypotheses"], "additionalProperties": False}},
}
STANDARD_CHUNKS = {"IHDR", "PLTE", "IDAT", "IEND", "tEXt", "zTXt", "iTXt", "pHYs", "gAMA", "sRGB", "cHRM", "bKGD",
                   "tIME", "tRNS", "iCCP", "sBIT", "hIST", "sPLT", "eXIf"}
CHUNK_RE = re.compile(r"\bchunk ([A-Za-z]{4})\b")
ANOMALY_RE = re.compile(r'"anomaly":\s*"([^"]{8,160})"')
USAGE_RE = re.compile(r"(?im)^(?:usage: (\S+)|(\S+): (?:unrecognized|invalid|unknown) option.*|.*?(\S+): command not found)")
ALWAYS_FAMILIAR = ("budget hit", "triage budget", "after iend", "trailer")  # covered by the base prompt


def unfamiliar_signals(output: str, exit_code: int, playbook: list[str]) -> list[str]:
    """Signals in a tool's output that the agent's own playbook doesn't mention yet.
    As the playbook learns them, the agent stops being nudged to search: familiarity is learned."""
    found: list[str] = []
    for c in CHUNK_RE.findall(output):
        if c not in STANDARD_CHUNKS:
            found.append(f"non-standard PNG chunk {c}")
    for a in ANOMALY_RE.findall(output):
        found.append(a)
    if exit_code != 0:
        for m in USAGE_RE.finditer(output):
            tool = next((g for g in m.groups() if g), None)
            if tool:
                found.append(f"how to use {tool.strip(':')}")
    book = " ".join(playbook).lower()

    def familiar(sig: str) -> bool:
        low = sig.lower()
        if any(f in low for f in ALWAYS_FAMILIAR):
            return True
        words = [w for w in re.findall(r"[a-z0-9-]{4,}", low) if w not in ("channel", "chunk", "with", "many", "values", "image")]
        return bool(words) and sum(w in book for w in words) >= max(1, len(words) // 2)

    out, seen = [], set()
    for f in found:
        if f not in seen and not familiar(f):
            seen.add(f)
            out.append(f)
    return out[:3]


EXTRACTED_RE = re.compile(r'"flag":\s*"(FLAG\{[A-Za-z0-9_]{4,64}\}|HS1:[^"]{1,120})",\s*"where":\s*"EXTRACTED')


@dataclass
class AttemptState:
    signals: list[dict] = field(default_factory=list)
    submitted: str | None = None
    extracted: dict[str, str] = field(default_factory=dict)  # flag -> lane that extracted it
    pending_tools: dict[str, dict] = field(default_factory=dict)
    tool_uses: dict[str, int] = field(default_factory=dict)
    branched: bool = False
    last_search: dict[str, dict] = field(default_factory=dict)  # lane -> search not yet followed by a command
    pending_trigger: dict[str, str] = field(default_factory=dict)  # lane -> unfamiliar signal awaiting a search
    nudged: set = field(default_factory=set)
    searches: list[dict] = field(default_factory=list)
    branches: list[dict] = field(default_factory=list)
    turns: int = 0


class ClaudeFinder:
    def __init__(self, cfg, search: SearchProvider, genome: FinderGenome | None = None):
        self.cfg = cfg
        self.search = search
        self.genome = genome or static_finder()
        self.client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key, max_retries=2)
        names = ["run_shell", "search_web", "fetch_page", "report_signal", "submit_flag"]
        if self.genome.learning:
            names += ["save_tool"] + (["branch"] if self.genome.max_branches >= 2 else [])
        self.tools = [TOOL_DEFS[n] for n in names]

    # ----------------------------------------------------------------- prompt
    def system(self, budget: float) -> str:
        s = BASE_SYSTEM.format(budget=budget)
        g = self.genome
        if not g.learning:
            return s
        playbook = "\n".join(f"- {b}" for b in g.playbook) or "(empty - this is your first round)"
        toolkit = "\n".join(f"- {n}: {t['description']} (used {t.get('uses', 0)}x, in {t.get('wins', 0)} cracks)"
                            for n, t in g.tools.items()) or "(empty)"
        return s + LEARNING_SYSTEM.format(persona=g.persona or "", playbook=playbook, toolkit=toolkit,
                                          max_branches=max(2, g.max_branches))

    # ------------------------------------------------------------------- run
    async def run(self, provider: SandboxProvider, sb: Sandbox, trace: Trace) -> dict:
        budget = self.cfg.finder_budget_s
        t0 = time.monotonic()
        deadline = t0 + budget
        st = AttemptState()
        trace.log("start", finder=self.genome.id, persona=self.genome.persona_name, model=self.cfg.finder_model,
                  budget_s=budget, sandbox=sb.id, provider=sb.provider, playbook_len=len(self.genome.playbook),
                  tools=list(self.genome.tools))
        messages: list = [{"role": "user", "content": "Begin. The image is ./challenge.png."}]
        stop = await self._loop(messages, provider, sb, trace, deadline, st, lane="main", system=self.system(budget))
        result = {"finder": self.genome.id, "persona": self.genome.persona_name, "model": self.cfg.finder_model,
                  "submitted_flag": st.submitted, "signals": st.signals, "turns": st.turns,
                  "elapsed_s": round(time.monotonic() - t0, 2), "stop_reason": stop,
                  "pending_tools": st.pending_tools, "tool_uses": st.tool_uses, "branches": st.branches,
                  "searches": st.searches,
                  "extracted": st.extracted}
        trace.log("end", **{k: v for k, v in result.items() if k in ("finder", "submitted_flag", "turns", "elapsed_s", "stop_reason")},
                  branched=st.branched, saved_tools=list(st.pending_tools))
        return result

    async def _loop(self, messages: list, provider, sb, trace, deadline: float, st: AttemptState, *, lane: str,
                    system: str, is_child: bool = False, stop_event: asyncio.Event | None = None) -> str:
        remaining = lambda: deadline - time.monotonic()  # noqa: E731
        child_submit: str | None = None
        while remaining() > 3 and st.turns < self.cfg.finder_max_turns * (1 if not is_child else 2):
            if stop_event is not None and stop_event.is_set():
                return "sibling_won"
            if not is_child and st.submitted is not None:
                return "submitted"
            st.turns += 1
            try:
                resp = await asyncio.wait_for(self._call(messages, system), timeout=max(5.0, remaining()))
            except (asyncio.TimeoutError, anthropic.APITimeoutError):
                trace.log("error", lane=lane, message="model call exceeded remaining budget")
                return "budget"
            except anthropic.APIError as e:
                trace.log("error", lane=lane, message=f"{type(e).__name__}: {e}")
                return "api_error"
            for b in resp.content:
                if b.type == "thinking" and getattr(b, "thinking", ""):
                    trace.log("reasoning", lane=lane, text=b.thinking)
                elif b.type == "text" and b.text.strip():
                    trace.log("text", lane=lane, text=b.text)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                trace.log("error", lane=lane, message="model refused")
                return "refusal"
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                return "end_turn"

            plain = [u for u in uses if u.name != "branch"]
            results = list(await asyncio.gather(*(self._tool(u, provider, sb, trace, deadline, st, lane, is_child) for u in plain)))
            for u in uses:
                if u.name == "branch":
                    results.append(await self._branch(u, messages, results, provider, sb, trace, deadline, st, lane, system, is_child))
            order = {u.id: i for i, u in enumerate(uses)}
            results.sort(key=lambda r: order[r["tool_use_id"]])
            if remaining() < 20:
                results.append({"type": "text", "text": f"[{remaining():.0f}s left: report_signal your best guess and submit_flag now.]"})
            messages.append({"role": "user", "content": results})
            for u in uses:
                if u.name == "submit_flag":
                    flag = str(u.input.get("flag", "")).strip()
                    if is_child:
                        child_submit = flag
                    else:
                        st.submitted = flag
            if is_child and (child_submit is not None or any(l == lane for l in st.extracted.values())):
                return "branch_done"
            if not is_child and st.submitted is not None:
                return "submitted"
        return "budget"

    async def _call(self, messages: list, system: str):
        return await self.client.beta.messages.create(
            model=self.cfg.finder_model,
            max_tokens=8000,
            system=system,
            tools=self.tools,
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},  # summaries feed the replay view
            output_config={"effort": self.cfg.finder_effort},
            cache_control={"type": "ephemeral"},  # same genome's attempts and branches share the prefix
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    # ------------------------------------------------------------------ tools
    async def _tool(self, u, provider, sb, trace, deadline, st: AttemptState, lane: str, is_child: bool) -> dict:
        extra = {}
        if u.name == "run_shell" and lane in st.last_search:  # link the command to the search that led to it
            extra["informed_by"] = st.last_search.pop(lane)
        trace.log("tool_call", lane=lane, tool=u.name, input=u.input, id=u.id, **extra)
        out, err = await self._exec_tool(u.name, u.input, provider, sb, trace, deadline, st, lane, is_child)
        return {"type": "tool_result", "tool_use_id": u.id, "content": out, "is_error": err}

    async def _exec_tool(self, name, inp, provider, sb, trace, deadline, st: AttemptState, lane, is_child) -> tuple[str, bool]:
        remaining = deadline - time.monotonic()
        try:
            if name == "run_shell":
                cmd = inp["command"]
                for t in self.genome.tools:
                    if f"mytools/{t}" in cmd:
                        st.tool_uses[t] = st.tool_uses.get(t, 0) + 1
                timeout = min(float(inp.get("timeout_s", 20)), 60.0, max(2.0, remaining - 2))
                res = await provider.exec(sb, cmd, timeout_s=timeout)
                out = res.for_model()
                for f in EXTRACTED_RE.findall(res.stdout):
                    st.extracted.setdefault(f, lane)
                trace.log("tool_result", lane=lane, tool=name, output=out, duration_s=round(res.duration_s, 2), exit_code=res.exit_code)
                nudge = ""
                # every finder can search; learning ones grow out of the nudges as their playbook covers them
                new = [x for x in unfamiliar_signals(res.stdout + "\n" + res.stderr, res.exit_code, self.genome.playbook)
                       if x not in st.nudged]
                if new:
                    st.nudged.update(new)
                    st.pending_trigger[lane] = new[0]
                    trace.log("unfamiliar", lane=lane, signals=new)
                    nudge = (f"\n[harness: unfamiliar signal(s) not covered by your playbook: {new}. "
                             f"If it matters for this image, one search_web on it before guessing.]")
                return out + nudge + f"\n[{deadline - time.monotonic():.0f}s left]", res.exit_code != 0
            if name == "search_web":
                hits = await self.search.search(inp["query"], max_results=5)
                trigger = st.pending_trigger.pop(lane, None)
                trace.log("search", lane=lane, provider=self.search.name, query=inp["query"],
                          titles=[h["title"] for h in hits], urls=[h.get("url") for h in hits], trigger=trigger)
                st.last_search[lane] = {"query": inp["query"], "search_i": trace.events, "provider": self.search.name,
                                        "trigger": trigger, "top": hits[0]["title"] if hits else None}
                st.searches.append({"query": inp["query"], "trigger": trigger, "lane": lane})
                return json.dumps(hits)[:6000], False
            if name == "fetch_page":
                page = await self.search.fetch(inp["url"])
                trace.log("fetch", lane=lane, provider=self.search.name, url=inp["url"], title=page.get("title"))
                if lane in st.last_search:
                    st.last_search[lane]["fetched"] = page.get("title") or inp["url"]
                return json.dumps(page)[:6000], False
            if name == "report_signal":
                sig = {"anomaly": inp.get("anomaly", ""), "technique": inp.get("technique"), "params": inp.get("params") or {}, "lane": lane}
                st.signals.append(sig)
                trace.log("signal", **sig)
                return "recorded", False
            if name == "submit_flag":
                trace.log("submit", lane=lane, flag=inp.get("flag"), confidence=inp.get("confidence"))
                return "submitted" if not is_child else "submitted to the parent agent", False
            if name == "save_tool":
                tname, code = inp["name"].strip().lower(), inp["code"]
                if not TOOL_NAME_RE.match(tname):
                    return "invalid name: use snake_case, 3-40 chars", True
                if len(code) > 20000:
                    return "tool too large (20k chars max)", True
                await provider.write_file(sb, f"{sb.tools_dir}/mytools/{tname}.py", code.encode())
                st.pending_tools[tname] = {"description": inp["description"][:300], "code": code, "lane": lane}
                trace.log("save_tool", lane=lane, name=tname, description=inp["description"][:300], lines=code.count("\n") + 1)
                return f"saved to $ARENA_TOOLS/mytools/{tname}.py (kept for future rounds if it validates)", False
            return f"unknown tool {name}", True
        except Exception as e:
            trace.log("tool_result", lane=lane, tool=name, output=f"error: {e}", error=True)
            return f"error: {type(e).__name__}: {e}", True

    # ----------------------------------------------------------------- branch
    async def _branch(self, u, messages, sibling_results, provider, sb, trace, deadline, st: AttemptState,
                      lane, system, is_child) -> dict:
        def res(text, err=False):
            return {"type": "tool_result", "tool_use_id": u.id, "content": text, "is_error": err}

        trace.log("tool_call", lane=lane, tool="branch", input=u.input, id=u.id)
        if is_child:
            return res("branches cannot branch again; pursue your hypothesis", True)
        if st.branched:
            return res("you already branched once this attempt", True)
        hyps = (u.input.get("hypotheses") or [])[: max(2, self.genome.max_branches)]
        if len(hyps) < 2 or deadline - time.monotonic() < 25:
            return res("need >=2 hypotheses and >25s remaining to branch (forking takes ~10s)", True)
        st.branched = True
        stop = asyncio.Event()
        child_deadline = deadline - 8  # leave the parent time to submit

        async def run_child(i: int, h: dict) -> dict:
            label = f"b{i}:{h['label'][:24]}"
            t0 = time.monotonic()
            try:
                child = await provider.fork(sb, name=f"{sb.id[:20]}-b{i}")
            except Exception as e:
                trace.log("error", lane=label, message=f"fork failed: {e}")
                return {"label": label, "error": str(e)}
            trace.log("fork", lane=label, sandbox=child.id, provider=child.provider, fork_s=round(time.monotonic() - t0, 3), parent_sandbox=sb.id)
            # Conversation fork: identical prefix (append-only), then this branch's own tool_result.
            cmsgs = list(messages)
            cmsgs.append({"role": "user", "content": [*sibling_results, {
                "type": "tool_result", "tool_use_id": u.id,
                "content": f"You are branch {label}. Pursue ONLY this hypothesis: {h['plan']}\nYour sandbox is a live fork "
                           f"(same files/state). Sibling branches pursue: {[x['label'] for x in hyps if x is not h]}. "
                           f"If you extract the flag, call submit_flag with it immediately. Deadline in "
                           f"{child_deadline - time.monotonic():.0f}s."}]})
            try:
                stop_reason = await self._loop(cmsgs, provider, child, trace, child_deadline, st, lane=label,
                                               system=system, is_child=True, stop_event=stop)
            finally:
                await provider.destroy(child)
            won = [f for f, ln in st.extracted.items() if ln == label]
            last = next((b.text for m in reversed(cmsgs) if m["role"] == "assistant"
                         for b in m["content"] if getattr(b, "type", "") == "text"), "")
            subs = [b.input.get("flag") for m in cmsgs if m["role"] == "assistant"
                    for b in m["content"] if getattr(b, "type", "") == "tool_use" and b.name == "submit_flag"]
            if won:
                stop.set()
            return {"label": label, "plan": h["plan"], "stop": stop_reason, "extracted": won,
                    "submitted": subs[-1] if subs else None, "last_note": last[:400],
                    "elapsed_s": round(time.monotonic() - t0, 1)}

        outcomes = await asyncio.gather(*(run_child(i, h) for i, h in enumerate(hyps)))
        st.branches = outcomes
        trace.log("branch_result", lane=lane, branches=outcomes)
        return res(json.dumps({"branches": outcomes, "signals_so_far": st.signals[-5:]}, default=str)[:6000])
