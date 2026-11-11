"""Post-round learning for a finder genome (same model - the agent changes, not the weights).

1. Reflection: one structured-output call per genome. It sees its own compact traces, the
   REVEALED ground truth for every scheme (spec + key), its current playbook and toolkit
   stats, and rewrites its playbook + decides which saved tools to keep.
2. Tool validation: kept tools are run inside ONE Archil fork per genome (model-written
   code never runs on the host); only tools that exit 0 are committed to the genome.
"""

from __future__ import annotations

import json
import logging

import anthropic
from pydantic import BaseModel, Field

from ..genome import FinderGenome
from ..sandbox.base import SandboxProvider
from ..store import ArenaStore

log = logging.getLogger(__name__)
MAX_LESSONS = 30


class Reflection(BaseModel):
    lessons: list[str] = Field(description=f"the full rewritten playbook: <= {MAX_LESSONS} short, concrete, actionable rules")
    keep_tools: list[str] = Field(description="names of newly saved tools worth keeping")
    retire_tools: list[str] = Field(description="names of existing toolkit tools that are useless or misleading")
    insight: str = Field(description="one sentence: the most important thing learned this round")


SYSTEM = """You are coaching yourself: a steganalysis agent that competes in timed rounds (90s per image). The model you run on never changes, so the ONLY way you get better is through this playbook and toolkit, which are injected into your prompt next round.

Given this round's attempts (what you ran, what happened) and the REVEALED ground truth for each image, rewrite your playbook:
- Keep rules that worked, fix or delete rules that misled you, add rules that would have cracked what you missed.
- Be concrete and operational: commands, flags, thresholds, orderings, time allocations, decoy patterns, hider tendencies (which techniques/keys the hiders favour).
- Prefer "detect X -> run Y" rules. When a web search taught you something useful, turn it into a rule so you won't need to search for it again (the harness stops flagging signals your playbook covers). Mention your own tools by name when they help.
- At most 30 rules. No fluff.
Keep a saved tool only if it is general (works on any image) and would have saved time."""


def compact_attempt(store: ArenaStore, res: dict) -> dict:
    """Commands + outcomes from the trace; enough to learn from, small enough to fit."""
    steps = []
    key = f"rounds/{res['round']}/attempts/{res['attempt_id']}/trace.jsonl"
    if store.exists(key):
        for line in store.read_bytes(key).decode().splitlines():
            e = json.loads(line)
            if e["type"] == "tool_call" and e.get("tool") == "run_shell":
                step = {"t": e["t"], "lane": e.get("lane"), "cmd": e["input"]["command"][:300]}
                if e.get("informed_by"):
                    step["after_search"] = e["informed_by"].get("query")
                steps.append(step)
            elif e["type"] == "tool_result" and e.get("tool") == "run_shell" and steps:
                steps[-1]["out"] = (e.get("output") or "")[:300]
            elif e["type"] in ("signal", "submit", "save_tool", "branch_result", "search", "unfamiliar"):
                steps.append({"t": e["t"], "lane": e.get("lane"), e["type"]: {k: v for k, v in e.items()
                              if k not in ("i", "ts", "t", "type", "lane")}})
    return {"scheme_id": res["scheme_id"], "cracked": res.get("cracked"), "score": res.get("score"),
            "decoy_submitted": res.get("decoy_submitted"), "elapsed_s": res.get("elapsed_s"),
            "stop": res.get("stop_reason"), "steps": steps[-25:]}


async def reflect(cfg, store: ArenaStore, genome: FinderGenome, attempts: list[dict], truths: dict[str, dict]) -> Reflection | None:
    if not genome.learning or not attempts:
        return None
    payload = {
        "current_playbook": genome.playbook,
        "toolkit": genome.tools,
        "new_tools_saved_this_round": {n: t["description"] for a in attempts for n, t in (a.get("pending_tools") or {}).items()},
        "attempts": [{**compact_attempt(store, a), "ground_truth": truths.get(a["scheme_id"])} for a in attempts],
    }
    client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)
    try:
        resp = await client.beta.messages.parse(
            model=cfg.finder_model, max_tokens=12000, system=SYSTEM + f"\n\nYour persona: {genome.persona}",
            messages=[{"role": "user", "content": json.dumps(payload, default=str)[:150_000]}],
            output_format=Reflection, thinking={"type": "adaptive"}, output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    except anthropic.APIError as e:
        log.warning("reflection failed for %s: %s", genome.id, e)
        return None
    r = resp.parsed_output
    if r is not None:
        r.lessons = [s.strip() for s in r.lessons if s.strip()][:MAX_LESSONS]
    return r


async def validate_tools(provider: SandboxProvider, template, tools: dict[str, str], sample_png: bytes,
                         timeout_s: float = 25) -> dict[str, bool]:
    """Run each tool once inside a fork; True if it exits 0."""
    if not tools:
        return {}
    sb = await provider.fork(template)
    try:
        files = {"challenge.png": sample_png}
        files.update({f"/opt/arena/mytools/{n}.py": code.encode() for n, code in tools.items()})
        await provider.write_bundle(sb, files)
        out = {}
        for n in tools:
            r = await provider.exec(sb, f"python3 $ARENA_TOOLS/mytools/{n}.py challenge.png", timeout_s=timeout_s)
            out[n] = r.exit_code == 0 and not r.timed_out
        return out
    finally:
        await provider.destroy(sb)


async def learn_from_round(cfg, store: ArenaStore, provider: SandboxProvider, template, genome: FinderGenome,
                           attempts: list[dict], truths: dict[str, dict], sample_png: bytes, generation: int = 0) -> dict:
    """Update genome in place (playbook, tool stats, committed tools) and persist it."""
    # usage / win stats for existing tools
    for a in attempts:
        for name, n in (a.get("tool_uses") or {}).items():
            if name in genome.tools:
                genome.tools[name]["uses"] = genome.tools[name].get("uses", 0) + n
                genome.tools[name]["wins"] = genome.tools[name].get("wins", 0) + (1 if a.get("cracked") else 0)
    r = await reflect(cfg, store, genome, attempts, truths)
    pending = {n: t for a in attempts for n, t in (a.get("pending_tools") or {}).items()}
    committed, rejected = [], []
    if r is not None:
        genome.playbook = r.lessons
        for name in r.retire_tools:
            genome.tools.pop(name, None)
        keep = {n: pending[n]["code"] for n in r.keep_tools if n in pending}
        ok = await validate_tools(provider, template, keep, sample_png)
        for n, good in ok.items():
            if good:
                genome.commit_tool(store, n, pending[n]["description"], pending[n]["code"])
                committed.append(n)
            else:
                rejected.append(n)
    genome.save(store, tag=f"gen{generation}")
    summary = {"genome": genome.id, "generation": generation, "insight": r.insight if r else None,
               "playbook_len": len(genome.playbook), "tools": list(genome.tools), "committed": committed,
               "rejected": rejected}
    store.append_jsonl(f"{genome.base}/learning.jsonl", summary)
    return summary
