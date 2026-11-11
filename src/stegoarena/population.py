"""Evolving populations: finder + hider genomes compete; the model never changes.

Each generation:
  1. every hider genome proposes schemes (its strategy + memory + the champion finder's
     PUBLIC playbook - the hider studies its opponent's notebook on the shared FS);
  2. every finder genome attacks every scheme once (one Archil fork each);
  3. Elo per finder genome / scheme; hider Elo = mean of its schemes;
  4. finders reflect on revealed ground truth -> playbook + validated toolkit; hiders log memory;
  5. selection: lowest-Elo finder and hider retire, replaced by a coached child of the champion.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time

import anthropic
from pydantic import BaseModel, Field

from .finder.learn import learn_from_round
from .generator.build import GenerationError, build_scheme
from .genome import FinderGenome, HiderGenome, child_finder, child_hider, seed_population
from .scorer import FINDER_START, EloBook, scheme_start_rating

log = logging.getLogger(__name__)


class Mutation(BaseModel):
    name: str = Field(description="short snake_case name for the new variant")
    text: str = Field(description="the new persona/strategy paragraph (3-6 sentences)")
    why: str = Field(description="one sentence: what changed and why")


async def coach(cfg, model: str, kind: str, champion: dict, loser: dict) -> Mutation | None:
    prompt = (f"Two {kind} agents competed in a steganography arena; the model is fixed, only their strategy text differs.\n"
              f"CHAMPION: {champion}\nRETIRED: {loser}\n"
              f"Write a new {kind} strategy that keeps what made the champion win, fixes its observed weaknesses, and "
              f"explores one genuinely different idea. Stay inside the rules (fixed technique family, solvable schemes).")
    try:
        r = await anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key).beta.messages.parse(
            model=model, max_tokens=4000, messages=[{"role": "user", "content": prompt}], output_format=Mutation,
            output_config={"effort": "low"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return r.parsed_output
    except anthropic.APIError as e:
        log.warning("coach failed: %s", e)
        return None


def _rating(elo: EloBook, key: str, default: float) -> float:
    return elo.data["ratings"].get(key, default)


async def evolve(arena, generations: int, schemes_per_hider: int = 2) -> dict:
    cfg, store = arena.cfg, arena.store
    pop = store.read_json("population.json") or seed_population(store)
    model_lock = (cfg.finder_model, cfg.finder_effort, cfg.hider_model)
    pop.setdefault("model_lock", list(model_lock))
    if tuple(pop["model_lock"]) != model_lock:
        raise RuntimeError(f"model/effort changed mid-run {pop['model_lock']} -> {model_lock}: that would be a model benchmark")
    template = await arena.template()

    for _ in range(generations):
        g = pop["generation"] + 1
        t0 = time.time()
        finders = [FinderGenome.load(store, fid) for fid in pop["finders"]]
        hiders = [HiderGenome.load(store, hid) for hid in pop["hiders"]]
        elo = EloBook(store)
        champion = max(finders, key=lambda f: _rating(elo, f"finder:{f.id}", FINDER_START))
        log.info("gen %d: finders=%s hiders=%s champion=%s", g, [f.id for f in finders], [h.id for h in hiders], champion.id)

        # 1. hiders propose (in parallel)
        from .hider.agent import ClaudeHider

        hider = ClaudeHider(cfg, arena.search)
        props = await asyncio.gather(*(hider.propose(g, schemes_per_hider, [], [], strategy=h.strategy, memory=h.memory,
                                                     opponent_playbook=champion.playbook, effort="medium") for h in hiders))
        built, owner = [], {}
        for h, plist in zip(hiders, props):
            for i, (spec, _parent, why) in enumerate(plist):
                sid = f"g{g}-{h.strategy_name[:6]}{i}"
                try:
                    b = build_scheme(store, g, sid, spec, seed=g * 1000 + len(built), rationale=f"[{h.id}] {why}")
                    built.append(b)
                    owner[sid] = h
                except GenerationError as e:
                    log.warning("discarded %s: %s", sid, e)

        # 2. every finder genome attacks every scheme once
        jobs = [arena.attempt(g, s, 0, f) for s in built for f in finders]
        results = await asyncio.gather(*jobs)

        # 3. Elo + summaries
        lineage = store.read_json("lineage.json", {"nodes": {}})
        summary = []
        for s in built:
            mine = [r for r in results if r["scheme_id"] == s.scheme_id]
            for r in mine:
                elo.update(round_no=g, finder=r["finder"], scheme=s.scheme_id, difficulty=s.difficulty, score=r["score"])
            best = max(mine, key=lambda r: r["score"]) if mine else {"score": 0, "cracked": False}
            node = {"scheme_id": s.scheme_id, "round": g, "parent_id": None, "hider": owner[s.scheme_id].id,
                    "spec": s.spec.to_dict(), "difficulty": s.difficulty, "cracked": bool(best.get("cracked")),
                    "cracked_by": sorted(r["finder"] for r in mine if r.get("cracked")),
                    "best_score": best["score"], "best_attempt": best.get("attempt_id"),
                    "attempts": [r["attempt_id"] for r in mine], "signals": best.get("signals", []),
                    "how": None, "rationale": s.rationale}
            lineage["nodes"][s.scheme_id] = {k: node[k] for k in ("round", "parent_id", "difficulty", "cracked", "best_score")}
            summary.append(node)
        for h in hiders:
            ratings = [_rating(elo, f"scheme:{n['scheme_id']}", scheme_start_rating(n["difficulty"]))
                       for n in summary if n["hider"] == h.id]
            if ratings:
                elo.data["ratings"][f"hider:{h.id}"] = round(statistics.mean(ratings), 2)
        elo.save()
        store.write_json("lineage.json", lineage)

        # 4. learning (finders reflect in parallel) + hider memory
        truths = {s.scheme_id: {**s.spec.to_dict(), "key": s.key, "decoy_flag_present": bool(s.decoy_flag)} for s in built}
        sample = store.read_bytes(built[0].image_key) if built else b""
        learned = await asyncio.gather(*(learn_from_round(cfg, store, arena.provider, template, f,
                                                          [r for r in results if r["finder"] == f.id], truths, sample, g)
                                         for f in finders))
        for h in hiders:
            for n in summary:
                if n["hider"] == h.id:
                    sp = n["spec"]
                    h.memory.append(f"g{g} {sp['technique']} order={sp['order']} cipher={sp['cipher']} key={sp['key_source']} "
                                    f"decoy={sp['decoy']}: cracked by {len(n['cracked_by'])}/{len(finders)} finders "
                                    f"(best score {n['best_score']:.2f})")
            h.save(store)

        # 5. selection (needs >=2 members)
        events = []
        if len(finders) >= 2:
            ranked = sorted(finders, key=lambda f: _rating(elo, f"finder:{f.id}", FINDER_START))
            loser, champ = ranked[0], ranked[-1]
            m = await coach(cfg, cfg.finder_model, "finder", {"persona": champ.persona, "playbook": champ.playbook[:10],
                            "elo": _rating(elo, f"finder:{champ.id}", FINDER_START)},
                            {"persona": loser.persona, "elo": _rating(elo, f"finder:{loser.id}", FINDER_START)})
            child = child_finder(store, champ, m.name if m else f"{champ.persona_name}_v{g}", m.text if m else champ.persona, g)
            pop["finders"] = [x for x in pop["finders"] if x != loser.id] + [child.id]
            pop["retired"].append(loser.id)
            events.append({"kind": "finder", "retired": loser.id, "parent": champ.id, "child": child.id, "why": m.why if m else None})
        if len(hiders) >= 2:
            hr = sorted(hiders, key=lambda h: _rating(elo, f"hider:{h.id}", 1000))
            loser, champ = hr[0], hr[-1]
            m = await coach(cfg, cfg.hider_model, "hider", {"strategy": champ.strategy, "memory": champ.memory[-8:]},
                            {"strategy": loser.strategy, "memory": loser.memory[-8:]})
            child = child_hider(champ, m.name if m else f"{champ.strategy_name}_v{g}", m.text if m else champ.strategy, g)
            child.save(store)
            pop["hiders"] = [x for x in pop["hiders"] if x != loser.id] + [child.id]
            pop["retired"].append(loser.id)
            events.append({"kind": "hider", "retired": loser.id, "parent": champ.id, "child": child.id, "why": m.why if m else None})

        pop["generation"] = g
        pop["history"].append({"generation": g, "t": time.time(), "elapsed_s": round(time.time() - t0, 1),
                               "learned": learned, "selection": events,
                               "ratings": {k: v for k, v in elo.data["ratings"].items() if not k.startswith("scheme:")}})
        store.write_json("population.json", pop)
        out = {"round": g, "started": t0, "elapsed_s": round(time.time() - t0, 1), "k": 1, "provider": arena.provider.name,
               "finders": [f.id for f in finders], "hider": ",".join(h.id for h in hiders), "search": arena.search.name,
               "schemes": summary, "learned": learned, "selection": events}
        store.write_json(f"rounds/{g}/summary.json", out)
        idx = store.read_json("rounds/index.json", {"rounds": []})
        idx["rounds"] = sorted(set(idx["rounds"]) | {g})
        store.write_json("rounds/index.json", idx)
        log.info("gen %d done in %.0fs: %s", g, time.time() - t0, events)
    return pop


def champion_finder(store) -> FinderGenome:
    pop = store.read_json("population.json")
    elo = EloBook(store)
    return max((FinderGenome.load(store, fid) for fid in pop["finders"]),
               key=lambda f: _rating(elo, f"finder:{f.id}", FINDER_START))
