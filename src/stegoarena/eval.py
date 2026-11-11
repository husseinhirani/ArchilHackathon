"""Frozen held-out evaluation: the same-model learning curve.

The eval set is built once (fixed seeds) and never shown to hiders or used for learning.
`run_eval` scores one finder genome on every eval scheme with learning switched off, so
static vs champion@gen0 vs champion@genN differ ONLY in playbook/tools/branching/persona.
"""

from __future__ import annotations

import asyncio
import statistics
import time

from .generator.build import BuiltScheme, build_scheme
from .genome import FinderGenome
from .arena_schemes import SchemeSpec, difficulty

EVAL_ROUND = 900  # eval schemes + attempts live under rounds/900/ (not in the round index)
EVAL_SPECS: list[dict] = [
    dict(technique="lsb", channels=["R", "G"], cipher="xor", key_source="metadata", decoy="trailer"),
    dict(technique="palette", palette_colors=64, order="prng", key_source="wordlist", decoy="metadata_text"),
    dict(technique="alpha", alpha_mode="alpha_lsb", order="prng", cipher="aes", key_source="wordlist"),
    dict(technique="dct", channels=["G"], dct_coeff="2,1", dct_delta=16, cipher="xor", key_source="wordlist", decoy="trailer"),
    dict(technique="dct", channels=["B"], dct_coeff="3,2", dct_delta=22, order="prng", cipher="aes", key_source="biglist"),
    dict(technique="lsb", channels=["G"], bit_plane=0, order="prng", cipher="xor", key_source="biglist", decoy="trailer"),
    dict(technique="dct", channels=["R"], dct_coeff="1,3", dct_delta=26, order="prng", cipher="xor", key_source="biglist", decoy="metadata_text"),
    dict(technique="lsb", channels=["B"], bit_plane=1, order="prng", cipher="aes", key_source="wordlist"),
]


def build_eval_set(store, n: int = 8, *, force: bool = False) -> list[str]:
    ids = []
    for i, spec in enumerate(EVAL_SPECS[:n]):
        sid = f"eval{i}"
        if force or not store.exists(f"rounds/{EVAL_ROUND}/schemes/{sid}/truth.json"):
            build_scheme(store, EVAL_ROUND, sid, spec, seed=424242 + i, rationale="frozen eval", random_cover=False)
        ids.append(sid)
    store.write_json("eval/set.json", {"schemes": ids, "built": time.time()})
    return ids


def load_eval_scheme(store, sid: str) -> BuiltScheme:
    base = f"rounds/{EVAL_ROUND}/schemes/{sid}"
    sp, tr = store.read_json(f"{base}/spec.json"), store.read_json(f"{base}/truth.json")
    spec = SchemeSpec.from_dict(sp)
    return BuiltScheme(EVAL_ROUND, sid, spec, tr["flag"], tr["key"], tr["decoy_flag"], difficulty(spec), f"{base}/image.png")


async def run_eval(arena, finder: FinderGenome | str, label: str, generation: int | None = None, repeats: int = 1) -> dict:
    store = arena.store
    ids = store.read_json("eval/set.json", {"schemes": []})["schemes"] or build_eval_set(store)
    schemes = [load_eval_scheme(store, sid) for sid in ids]
    results = await asyncio.gather(*(arena.attempt(EVAL_ROUND, s, k, finder) for s in schemes for k in range(repeats)))
    cracked = [r for r in results if r.get("cracked")]
    out = {
        "label": label, "finder": getattr(finder, "id", finder), "persona": getattr(finder, "persona_name", finder),
        "generation": generation, "model": arena.cfg.finder_model, "effort": arena.cfg.finder_effort,
        "playbook_len": len(getattr(finder, "playbook", []) or []), "tools": list(getattr(finder, "tools", {}) or {}),
        "n": len(results), "repeats": repeats, "crack_rate": round(len(cracked) / max(1, len(results)), 3),
        "mean_score": round(statistics.mean(r["score"] for r in results), 3) if results else 0,
        "mean_time_to_crack_s": round(statistics.mean(r.get("elapsed_s") or 0 for r in cracked), 1) if cracked else None,
        "decoys_taken": sum(1 for r in results if r.get("decoy_submitted")),
        "per_scheme": {sid: {"crack_rate": round(sum(1 for r in rs if r.get("cracked")) / len(rs), 2),
                             "mean_score": round(statistics.mean(r["score"] for r in rs), 3),
                             "attempts": [r["attempt_id"] for r in rs]}
                       for sid in ids for rs in [[r for r in results if r["scheme_id"] == sid]] if rs},
        "t": time.time(),
    }
    store.write_json(f"eval/results/{label}.json", out)
    idx = store.read_json("eval/index.json", {"runs": []})
    idx["runs"] = [x for x in idx["runs"] if x != label] + [label]
    store.write_json("eval/index.json", idx)
    return out
