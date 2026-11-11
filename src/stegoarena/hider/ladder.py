"""Curated difficulty ladder + mutation operator. Used offline (no API key), as the
fallback when the LLM hider's specs fail validation, and for round 1 seeding."""

from __future__ import annotations

import random

from ..arena_schemes import SchemeSpec, SpecError, difficulty, validate_spec

KNOWN_EASY = dict(technique="lsb", channels=["R", "G", "B"], bit_plane=0, order="sequential")

LADDER: list[dict] = [
    KNOWN_EASY,
    dict(technique="alpha", alpha_mode="transparent_rgb"),
    dict(technique="lsb", channels=["B"], bit_plane=1, decoy="trailer"),
    dict(technique="palette", palette_colors=64, decoy="metadata_text"),
    dict(technique="lsb", channels=["R", "G"], cipher="xor", key_source="metadata"),
    dict(technique="dct", channels=["G"], dct_coeff="2,1", dct_delta=16),
    dict(technique="alpha", alpha_mode="alpha_lsb", order="prng", key_source="metadata", decoy="trailer"),
    dict(technique="lsb", channels=["G"], bit_plane=0, order="prng", cipher="aes", key_source="wordlist"),
    dict(technique="dct", channels=["B"], dct_coeff="3,2", dct_delta=24, order="prng", key_source="wordlist", decoy="metadata_text"),
    dict(technique="palette", palette_colors=128, order="prng", cipher="aes", key_source="biglist"),
    dict(technique="dct", channels=["R"], dct_coeff="2,3", dct_delta=18, order="prng", cipher="xor", key_source="biglist", decoy="trailer"),
]

MUTATIONS = {
    "technique": ["lsb", "dct", "palette", "alpha"],
    "channels": [["R"], ["G"], ["B"], ["R", "G"], ["G", "B"], ["R", "G", "B"]],
    "bit_plane": [0, 1, 2],
    "order": ["sequential", "prng"],
    "dct_coeff": ["1,2", "2,1", "2,2", "3,1", "1,3", "3,2", "2,3"],
    "dct_delta": [12, 14, 16, 20, 24, 28],
    "palette_colors": [32, 64, 128, 256],
    "alpha_mode": ["alpha_lsb", "transparent_rgb"],
    "cipher": ["none", "xor", "aes"],
    "key_source": ["metadata", "wordlist", "biglist"],
    "decoy": ["none", "metadata_text", "trailer"],
}


def mutate(spec: SchemeSpec | dict, rng: random.Random, *, harder: bool = True, n: int = 2) -> SchemeSpec:
    """Change n fields; if harder=True keep the mutation only if difficulty doesn't drop."""
    base = validate_spec(dict(spec.to_dict() if isinstance(spec, SchemeSpec) else spec))
    for _ in range(50):
        d = base.to_dict()
        for field in rng.sample(list(MUTATIONS), n):
            d[field] = rng.choice(MUTATIONS[field])
        if d["order"] == "prng" or d["cipher"] != "none":
            if d.get("key_source", "none") == "none":
                d["key_source"] = rng.choice(MUTATIONS["key_source"])
        try:
            cand = validate_spec(d)
        except SpecError:
            continue
        if not harder or difficulty(cand) > difficulty(base):
            return cand
    return base


def ladder_specs(round_no: int, n: int, survivors: list[dict], cracked: list[dict], seed: int) -> list[tuple[SchemeSpec, str | None, str]]:
    """Return [(spec, parent_scheme_id, rationale)]. Round 1 walks the ladder; later rounds
    keep survivors' lineage and mutate cracked schemes to be harder."""
    rng = random.Random(seed)
    out: list[tuple[SchemeSpec, str | None, str]] = []
    if round_no == 1 or not (survivors or cracked):
        step = max(1, len(LADDER) // n)
        for i in range(n):
            out.append((validate_spec(dict(LADDER[min(i * step, len(LADDER) - 1)])), None, "curated ladder rung"))
        return out
    for s in survivors:
        if len(out) >= n:
            break
        out.append((mutate(s["spec"], rng, harder=False, n=1), s["scheme_id"], "survivor: small variation of an unbroken scheme"))
    for c in cracked:
        if len(out) >= n:
            break
        out.append((mutate(c["spec"], rng, harder=True, n=2), c["scheme_id"], "cracked last round: mutated harder"))
    while len(out) < n:
        out.append((mutate(rng.choice(LADDER), rng, harder=True), None, "fresh ladder mutation"))
    return out
