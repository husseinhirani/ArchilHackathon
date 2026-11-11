"""Generic triage: `python -m arena_schemes.triage challenge.png [--keys k1,k2] [--wordlist] [--quick]`

Prints JSON: {"flags": [{"flag", "where"}], "signals": [...], "metadata": {...}}.
--quick: raw-byte scan + unkeyed sequential extraction only (the scripted baseline finder).
Otherwise also tries metadata strings (and --keys / --wordlist) as keys with prng order + ciphers.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

from .api import FLAG_RE, extract, load_png
from .spec import ALPHA_MODES, CIPHERS, DCT_COEFFS, SchemeSpec

RAW_FLAG = re.compile(rb"FLAG\{[A-Za-z0-9_]{4,64}\}")
CHANNEL_SETS = [["R", "G", "B"], ["R"], ["G"], ["B"], ["R", "G"], ["R", "B"], ["G", "B"]]


def candidate_specs(img, *, keyed: bool):
    orders = ["sequential", "prng"] if keyed else ["sequential"]
    ciphers = list(CIPHERS) if keyed else ["none"]
    for order, cipher in itertools.product(orders, ciphers):
        if keyed and order == "sequential" and cipher == "none":
            continue  # already covered by the unkeyed pass
        ks = "metadata" if keyed else "none"
        base = dict(order=order, cipher=cipher, key_source=ks)
        if img.mode == "P":
            yield SchemeSpec(technique="palette", **base)
            continue
        for chans in CHANNEL_SETS:
            for bp in (0, 1, 2):
                yield SchemeSpec(technique="lsb", channels=chans, bit_plane=bp, **base)
        if img.mode == "RGBA":
            for mode in ALPHA_MODES:
                yield SchemeSpec(technique="alpha", alpha_mode=mode, **base)
        for ch in ("R", "G", "B"):
            for coeff in DCT_COEFFS:
                for delta in range(12, 33, 2):
                    yield SchemeSpec(technique="dct", channels=[ch], dct_coeff=coeff, dct_delta=delta, **base)


def lsb_signals(img) -> list[dict]:
    """Cheap statistical anomaly checks (pairs-of-values style)."""
    out = []
    if img.mode == "P":
        out.append({"anomaly": "palette image (indexed colour)", "technique": "palette"})
        return out
    arr = np.array(img.convert("RGBA"), dtype=np.int64)
    for i, ch in enumerate("RGB"):
        v = arr[:, :, i].reshape(-1)
        hist = np.bincount(v, minlength=256)
        even, odd = hist[0::2], hist[1::2]
        tot = even + odd
        mask = tot > 20
        if mask.sum() < 8:
            continue
        # chi-square of PoV: embedded LSBs equalise histogram pairs
        exp = tot[mask] / 2
        chi = float((((even[mask] - exp) ** 2) / exp).sum() / mask.sum())
        if chi < 0.6:
            out.append({"anomaly": f"LSB pairs-of-values equalised in {ch} (chi={chi:.2f})", "technique": "lsb", "params": {"channel": ch}})
    a = arr[:, :, 3]
    if img.mode == "RGBA":
        if np.isin(a, [254]).mean() > 0.05:
            out.append({"anomaly": "alpha channel has many 254 values (alpha LSB)", "technique": "alpha", "params": {"alpha_mode": "alpha_lsb"}})
        rows = (a == 0).all(axis=1)
        if rows.any() and arr[rows, :, :3].std() > 30:
            out.append({"anomaly": "fully transparent rows carry high-entropy RGB", "technique": "alpha", "params": {"alpha_mode": "transparent_rgb"}})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--keys", default="", help="comma-separated extra keys")
    ap.add_argument("--wordlist", action="store_true", help="also try every word in wordlist.txt (slow)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--budget", type=float, default=45.0, help="seconds")
    a = ap.parse_args(argv)
    t0 = time.time()
    raw = Path(a.image).read_bytes()
    img = load_png(raw)
    flags, seen = [], set()

    def add(flag, where, spec=None, key=None):
        if flag not in seen:
            seen.add(flag)
            flags.append({"flag": flag, "where": where, "spec": spec.to_dict() if spec else None, "key": key})

    iend = raw.find(b"IEND")
    for m in RAW_FLAG.finditer(raw):
        where = "after IEND (trailer)" if iend != -1 and m.start() > iend else "raw bytes / metadata chunk"
        add(m.group(0).decode(), where)
    meta = {k: str(v) for k, v in (img.info or {}).items() if isinstance(v, (str, bytes))}
    signals = [] if a.quick else lsb_signals(img)

    for spec in candidate_specs(img, keyed=False):
        if (f := extract(img, spec, None)):
            add(f, f"extracted via {spec.technique}", spec)
    if not a.quick:
        keys = [v for v in meta.values() if v and not FLAG_RE.search(v)] + [k for k in a.keys.split(",") if k]
        if a.wordlist:
            keys += (Path(__file__).parent / "wordlist.txt").read_text().split()
        for key in keys:
            for spec in candidate_specs(img, keyed=True):
                if time.time() - t0 > a.budget:
                    signals.append({"anomaly": "triage budget exhausted", "technique": None})
                    break
                if (f := extract(img, spec, key)):
                    add(f, f"extracted via {spec.technique} with key {key!r}", spec, key)
    json.dump({"flags": flags, "signals": signals, "metadata": meta, "elapsed_s": round(time.time() - t0, 2)}, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
