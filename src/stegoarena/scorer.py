"""Partial-credit scoring + Elo between hider schemes and finder versions."""

from __future__ import annotations

import time

from .store import ArenaStore

WEIGHTS = {"detection": 0.15, "technique": 0.20, "params": 0.20, "flag": 0.45}
DECOY_PENALTY = 0.15
KEYWORDS = {"lsb": ["lsb", "least significant", "bit plane", "bitplane", "pairs"],
            "dct": ["dct", "frequency", "coefficient", "qim", "jpeg"],
            "palette": ["palette", "index", "indexed", "ezstego"],
            "alpha": ["alpha", "transparen"]}
PARAM_KEYS = {"lsb": ["channels", "bit_plane", "order"], "dct": ["channels", "dct_coeff", "dct_delta", "order"],
              "palette": ["order"], "alpha": ["alpha_mode", "order"]}


def _norm(v):
    if isinstance(v, list):
        return sorted(str(x).upper() for x in v)
    if isinstance(v, str) and v.upper() in ("R", "G", "B"):
        return [v.upper()]
    return str(v).lower()


def score_attempt(result: dict, spec: dict, truth: dict) -> dict:
    tech = spec["technique"]
    signals = result.get("signals") or []
    detection = any(s.get("technique") == tech or any(k in (s.get("anomaly") or "").lower() for k in KEYWORDS[tech]) for s in signals)
    technique = any(s.get("technique") == tech for s in signals)
    best_params = 0.0
    keys = PARAM_KEYS[tech] + (["cipher"] if spec.get("cipher", "none") != "none" else [])
    for s in signals:
        if s.get("technique") != tech:
            continue
        p = s.get("params") or {}
        if "channel" in p and "channels" not in p:
            p = {**p, "channels": p["channel"]}
        hits = sum(1 for k in keys if k in p and _norm(p[k]) == _norm(spec.get(k)))
        best_params = max(best_params, hits / len(keys))
    sub = (result.get("submitted_flag") or "").strip()
    flag = sub == truth["flag"]
    decoy = bool(truth.get("decoy_flag")) and sub == truth["decoy_flag"]
    if flag:  # recovering the payload is impossible without the right technique + params
        detection = technique = True
        best_params = 1.0
    comps = {"detection": float(detection), "technique": float(technique), "params": round(best_params, 3), "flag": float(flag)}
    total = sum(WEIGHTS[k] * v for k, v in comps.items()) - (DECOY_PENALTY if decoy else 0.0)
    return {"components": comps, "decoy_submitted": decoy, "cracked": flag, "score": round(max(0.0, min(1.0, total)), 4)}


# ------------------------------------------------------------------ Elo
K_FACTOR = 32
FINDER_START = 1200.0


def scheme_start_rating(difficulty: float) -> float:
    return 1000.0 + 40.0 * difficulty  # harder schemes enter higher on the ladder


class EloBook:
    """elo.json = {"ratings": {player: rating}, "history": [{t, round, finder, scheme, score, ...}]}"""

    def __init__(self, store: ArenaStore):
        self.store = store
        self.data = store.read_json("elo.json", {"ratings": {}, "meta": {}, "history": []})

    def rating(self, player: str, default: float) -> float:
        return self.data["ratings"].setdefault(player, default)

    def update(self, *, round_no: int, finder: str, scheme: str, difficulty: float, score: float) -> dict:
        rf = self.rating(f"finder:{finder}", FINDER_START)
        rs = self.rating(f"scheme:{scheme}", scheme_start_rating(difficulty))
        exp_f = 1.0 / (1.0 + 10 ** ((rs - rf) / 400.0))
        delta = K_FACTOR * (score - exp_f)
        self.data["ratings"][f"finder:{finder}"] = round(rf + delta, 2)
        self.data["ratings"][f"scheme:{scheme}"] = round(rs - delta, 2)
        self.data["meta"].setdefault(f"scheme:{scheme}", {"round": round_no, "difficulty": difficulty})
        ev = {"t": time.time(), "round": round_no, "finder": finder, "scheme": scheme, "score": score,
              "expected": round(exp_f, 3), "finder_rating": self.data["ratings"][f"finder:{finder}"],
              "scheme_rating": self.data["ratings"][f"scheme:{scheme}"]}
        self.data["history"].append(ev)
        return ev

    def save(self) -> None:
        self.store.write_json("elo.json", self.data)
