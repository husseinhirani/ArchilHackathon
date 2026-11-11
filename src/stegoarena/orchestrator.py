"""Round loop: hider proposes N schemes -> generator builds + round-trips them ->
K finder forks per scheme in parallel -> best attempt scored -> Elo + lineage ->
survivors/cracked feed the next generation."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .arena_schemes import validate_spec
from .finder.scripted import VERSION as SCRIPTED_VERSION
from .finder.scripted import run_scripted
from .finder.search import make_search
from .finder.trace import Trace
from .generator.build import BuiltScheme, GenerationError, build_scheme
from .hider.ladder import ladder_specs
from .sandbox.base import Sandbox, SandboxProvider, make_provider
from .scorer import EloBook, score_attempt
from .store import ArenaStore, MirrorStore, make_store

log = logging.getLogger(__name__)


class Arena:
    def __init__(self, cfg, *, finder: str = "llm", hider: str = "llm"):
        self.cfg = cfg
        self.store: ArenaStore = make_store(cfg)
        self.provider: SandboxProvider = make_provider(cfg)
        self.search = make_search(cfg)
        self.finder_kind = finder if (finder != "llm" or cfg.anthropic_api_key) else "scripted"
        self.hider_kind = hider if (hider != "llm" or cfg.anthropic_api_key) else "ladder"
        if self.finder_kind != finder or self.hider_kind != hider:
            log.warning("ANTHROPIC_API_KEY missing: finder=%s hider=%s", self.finder_kind, self.hider_kind)
        self.sem = asyncio.Semaphore(cfg.max_concurrency)
        self._template: Sandbox | None = None
        self._template_lock = asyncio.Lock()

    # ------------------------------------------------------------ attempts
    async def template(self) -> Sandbox:
        async with self._template_lock:
            return await self._template_unlocked()

    async def _template_unlocked(self) -> Sandbox:
        if self._template is None:
            t0 = time.monotonic()
            self._template = await self.provider.create_from_template()
            log.info("template ready (%s) in %.1fs", self.provider.name, time.monotonic() - t0)
        return self._template

    async def attempt(self, round_no: int, scheme: BuiltScheme, k_index: int, finder, attempt_id: str | None = None) -> dict:
        """finder: "scripted" | "llm" (static Claude) | FinderGenome."""
        from .genome import FinderGenome, static_finder

        genome = finder if isinstance(finder, FinderGenome) else (static_finder() if finder == "llm" else None)
        tag = genome.id if genome else "scripted"
        attempt_id = attempt_id or f"{scheme.scheme_id}-{tag}-k{k_index}-{uuid.uuid4().hex[:4]}"
        trace = Trace(self.store, round_no, attempt_id)
        async with self.sem:
            trace.t0 = time.time()  # replay clock starts when the attempt leaves the queue
            t_fork = time.monotonic()
            try:
                sb = await self.provider.fork(await self.template(), name=f"sa-{attempt_id}"[:60])
            except Exception as e:
                log.exception("fork failed for %s", attempt_id)
                trace.log("error", message=f"fork failed: {e}")
                result = {"finder": tag, "submitted_flag": None, "signals": [], "stop_reason": "fork_failed",
                          "attempt_id": attempt_id, "scheme_id": scheme.scheme_id, "round": round_no, "score": 0.0,
                          "cracked": False, "components": {}, "decoy_submitted": False}
                trace.result(result)
                return result
            fork_s = time.monotonic() - t_fork
            trace.log("fork", sandbox=sb.id, provider=sb.provider, fork_s=round(fork_s, 3))
            try:
                files = {"challenge.png": self.store.read_bytes(scheme.image_key)}
                if genome is not None and genome.learning:  # the genome's toolkit rides along in the same upload
                    files.update({f"/opt/arena/mytools/{n}": b for n, b in genome.tool_files(self.store).items()})
                t_up = time.monotonic()
                await self.provider.write_bundle(sb, files)
                trace.log("upload", files=len(files), upload_s=round(time.monotonic() - t_up, 3))
                if genome is not None:
                    from .finder.agent import ClaudeFinder

                    result = await ClaudeFinder(self.cfg, self.search, genome).run(self.provider, sb, trace)
                else:
                    result = await run_scripted(self.provider, sb, trace)
            except Exception as e:
                log.exception("attempt %s crashed", attempt_id)
                trace.log("error", message=f"attempt crashed: {e}")
                result = {"finder": tag, "submitted_flag": None, "signals": [], "stop_reason": "crash"}
            finally:
                await self.provider.destroy(sb)
        spec = scheme.spec.to_dict()
        truth = self.store.read_json(f"rounds/{round_no}/schemes/{scheme.scheme_id}/truth.json")
        result.update(attempt_id=attempt_id, scheme_id=scheme.scheme_id, round=round_no, sandbox=sb.id,
                      provider=sb.provider, fork_s=round(fork_s, 3), **score_attempt(result, spec, truth))
        trace.log("scored", score=result["score"], cracked=result["cracked"], components=result["components"])
        trace.result(result)
        return result

    # --------------------------------------------------------------- round
    async def propose(self, round_no: int, n: int, feedback: list[dict]) -> tuple[list, list]:
        research: list[dict] = []
        if self.hider_kind == "llm":
            from .hider.agent import ClaudeHider

            hider = ClaudeHider(self.cfg, self.search)
            research = await hider.research(feedback)
            return await hider.propose(round_no, n, feedback, research), research
        survivors = [f for f in feedback if not f["cracked"]]
        cracked = [f for f in feedback if f["cracked"]]
        return ladder_specs(round_no, n, survivors, cracked, seed=round_no), research

    async def run_round(self, round_no: int, n_schemes: int, k: int, finder_kinds: list[str] | None = None) -> dict:
        finder_kinds = finder_kinds or [self.finder_kind]
        t0 = time.time()
        feedback = self.store.read_json(f"rounds/{round_no - 1}/summary.json", {}).get("schemes", []) if round_no > 1 else []
        proposals, research = await self.propose(round_no, n_schemes, feedback)
        if research:
            self.store.write_json(f"rounds/{round_no}/hider_research.json", research)

        built: list[BuiltScheme] = []
        for i, (spec, parent, why) in enumerate(proposals):
            sid = f"r{round_no}s{i}"
            try:
                built.append(build_scheme(self.store, round_no, sid, spec, seed=round_no * 1000 + i, parent_id=parent, rationale=why))
            except GenerationError as e:
                log.warning("discarded %s: %s", sid, e)
        log.info("round %d: %d schemes built; fanning out K=%d x %s", round_no, len(built), k, finder_kinds)

        jobs = [self.attempt(round_no, s, j, fk) for s in built for fk in finder_kinds for j in range(k)]
        results = await asyncio.gather(*jobs)

        elo = EloBook(self.store)
        lineage = self.store.read_json("lineage.json", {"nodes": {}})
        summary = []
        for s in built:
            mine = [r for r in results if r["scheme_id"] == s.scheme_id]
            for fid in sorted({r.get("finder") for r in mine}):
                fv = [r for r in mine if r.get("finder") == fid]
                best = max(fv, key=lambda r: r["score"])
                elo.update(round_no=round_no, finder=fid, scheme=s.scheme_id, difficulty=s.difficulty, score=best["score"])
            best = max(mine, key=lambda r: r["score"]) if mine else {"score": 0, "cracked": False, "signals": []}
            how = None
            if best.get("cracked"):
                how = f"{best['finder']} recovered the flag in {best.get('elapsed_s')}s ({best.get('turns')} turns)"
            node = {"scheme_id": s.scheme_id, "round": round_no, "parent_id": s.parent_id, "spec": s.spec.to_dict(),
                    "difficulty": s.difficulty, "cracked": bool(best.get("cracked")), "best_score": best["score"],
                    "best_attempt": best.get("attempt_id"), "attempts": [r["attempt_id"] for r in mine],
                    "signals": best.get("signals", []), "how": how, "rationale": s.rationale}
            lineage["nodes"][s.scheme_id] = {k_: node[k_] for k_ in ("round", "parent_id", "difficulty", "cracked", "best_score")}
            summary.append(node)
        elo.save()
        self.store.write_json("lineage.json", lineage)
        out = {"round": round_no, "started": t0, "elapsed_s": round(time.time() - t0, 1), "k": k, "provider": self.provider.name,
               "finders": finder_kinds, "hider": self.hider_kind, "search": self.search.name, "schemes": summary}
        self.store.write_json(f"rounds/{round_no}/summary.json", out)
        index = self.store.read_json("rounds/index.json", {"rounds": []})
        index["rounds"] = sorted(set(index["rounds"]) | {round_no})
        self.store.write_json("rounds/index.json", index)
        return out

    async def close(self) -> None:
        await self.provider.close()
        if isinstance(self.store, MirrorStore):
            self.store.flush()


async def smoke(cfg, finder: str = "scripted") -> dict:
    """Known-easy scheme must be cracked by the scripted finder. If not: tooling bug."""
    arena = Arena(cfg, finder=finder, hider="ladder")
    from .hider.ladder import KNOWN_EASY

    s = build_scheme(arena.store, 0, "smoke", validate_spec(dict(KNOWN_EASY)), seed=1, rationale="smoke test")
    try:
        return await arena.attempt(0, s, 0, finder if finder == "llm" else "scripted")
    finally:
        await arena.close()


__all__ = ["Arena", "smoke", "SCRIPTED_VERSION"]
