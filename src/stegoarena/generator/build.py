"""Generator: spec -> embedded image + ground truth on the shared FS.

A scheme that fails its own round-trip is discarded (raises GenerationError)."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path

from ..arena_schemes import SchemeSpec, difficulty, embed, extract, load_png, save_png, validate_spec
from ..store import ArenaStore
from .covers import pick_cover

WORDLIST = (Path(__file__).resolve().parents[1] / "arena_schemes" / "wordlist.txt").read_text().split()


class GenerationError(RuntimeError):
    pass


@dataclass
class BuiltScheme:
    round: int
    scheme_id: str
    spec: SchemeSpec
    flag: str
    key: str | None
    decoy_flag: str | None
    difficulty: float
    image_key: str
    parent_id: str | None = None
    rationale: str = ""


def new_flag() -> str:
    return f"FLAG{{{secrets.token_hex(8)}}}"


def make_key(spec: SchemeSpec, rng_seed: int) -> str | None:
    h = int(hashlib.sha256(str(rng_seed).encode()).hexdigest(), 16)
    if spec.key_source == "none":
        return None
    if spec.key_source in ("metadata", "wordlist"):
        return WORDLIST[h % len(WORDLIST)]
    if spec.key_source == "biglist":
        return f"{WORDLIST[h % len(WORDLIST)]}{(h // 7) % 100:02d}"
    return secrets.token_hex(12)  # "secret": never written anywhere the finder can see


def build_scheme(
    store: ArenaStore,
    round_no: int,
    scheme_id: str,
    spec: SchemeSpec | dict,
    *,
    seed: int,
    parent_id: str | None = None,
    rationale: str = "",
    cover_size: int = 512,
    retries: int = 3,
    allow_secret: bool = False,
) -> BuiltScheme:
    spec = validate_spec(spec, allow_secret=allow_secret)  # solvability gate
    last_err: Exception | None = None
    for attempt in range(retries):
        s = seed * 101 + attempt
        cover, cover_name = pick_cover(s, cover_size)
        flag = new_flag()
        key = make_key(spec, s)
        decoy = new_flag() if spec.decoy != "none" else None
        try:
            data = save_png(embed(cover, flag, spec, key), spec, key, decoy)
            if extract(load_png(data), spec, key) != flag:
                raise GenerationError("round-trip extraction mismatch")
        except Exception as e:  # capacity, quantiser edge cases, ...
            last_err = e
            continue
        base = f"rounds/{round_no}/schemes/{scheme_id}"
        store.write_bytes(f"{base}/image.png", data)
        store.write_json(f"{base}/spec.json", {"scheme_id": scheme_id, "round": round_no, "parent_id": parent_id,
                                               "rationale": rationale, "difficulty": difficulty(spec), **spec.to_dict()})
        store.write_json(f"{base}/truth.json", {"flag": flag, "key": key, "decoy_flag": decoy, "cover": cover_name, "seed": s})
        return BuiltScheme(round_no, scheme_id, spec, flag, key, decoy, difficulty(spec), f"{base}/image.png", parent_id, rationale)
    raise GenerationError(f"scheme {scheme_id} failed round-trip after {retries} tries: {last_err}")
