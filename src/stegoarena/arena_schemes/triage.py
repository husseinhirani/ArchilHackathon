"""Generic triage: `python -m arena_schemes.triage challenge.png [--keys k1,k2] [--wordlist] [--quick]`

Prints JSON: {"flags": [{"flag", "where"}], "signals": [...], "metadata": {...}}.
--quick: raw-byte scan + unkeyed sequential extraction only (the scripted baseline finder).
Otherwise: fast search over every carrier (lsb/dct/palette/alpha params) x order x cipher, with keys from
PNG metadata, --keys, and optionally the bundled --wordlist. Narrow with --only lsb,dct.
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

from .api import FLAG_RE, MSG_PREFIX, extract, load_png
from .bruteforce import bigwordlist, search, wordlist
from .spec import ALPHA_MODES, CIPHERS, DCT_COEFFS, SchemeSpec

RAW_FLAG = re.compile(rb"FLAG\{[A-Za-z0-9_]{4,64}\}|HS1:[ -~]{1,120}")
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


def chi_by_channel(img) -> dict[str, float | None]:
    """Pairs-of-values chi per RGB channel: ~1+ for natural images, near 0 when LSBs were overwritten
    (embedding equalises each (2k, 2k+1) histogram pair). None when the channel has too few populated pairs."""
    arr = np.array(img.convert("RGB"), dtype=np.int64)
    out: dict[str, float | None] = {}
    for i, ch in enumerate("RGB"):
        hist = np.bincount(arr[:, :, i].reshape(-1), minlength=256)
        even, odd = hist[0::2], hist[1::2]
        tot = even + odd
        mask = tot > 20
        if mask.sum() < 8:
            out[ch] = None
            continue
        exp = tot[mask] / 2
        out[ch] = float((((even[mask] - exp) ** 2) / exp).sum() / mask.sum())
    return out


def lsb_signals(img) -> list[dict]:
    """Cheap statistical anomaly checks (pairs-of-values style)."""
    out = []
    if img.mode == "P":
        out.append({"anomaly": "palette image (indexed colour)", "technique": "palette"})
        return out
    arr = np.array(img.convert("RGBA"), dtype=np.int64)
    for ch, chi in chi_by_channel(img).items():
        if chi is not None and chi < 0.6:
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
    ap = argparse.ArgumentParser(description="Steganalysis triage for arena_schemes images.")
    ap.add_argument("image")
    ap.add_argument("--keys", default="", help="comma-separated extra keys to try")
    ap.add_argument("--wordlist", action="store_true", help="also try every word in wordlist.txt as the key (fast, ~seconds)")
    ap.add_argument("--biglist", action="store_true", help="try ~16k word+NN keys (slow unless narrowed)")
    ap.add_argument("--only", default="", help="restrict to techniques, e.g. lsb,dct")
    ap.add_argument("--channels", default="", help="narrow: e.g. G or RB (lsb exact set; dct channel)")
    ap.add_argument("--bit-planes", default="", help="narrow lsb: e.g. 0,1")
    ap.add_argument("--deltas", default="", help="narrow dct: e.g. 16,20")
    ap.add_argument("--coeffs", default="", help="narrow dct: e.g. 2,1;3,2")
    ap.add_argument("--quick", action="store_true", help="raw-byte scan + unkeyed sequential extraction only")
    ap.add_argument("--budget", type=float, default=45.0, help="seconds")
    a = ap.parse_args(argv)
    t0 = time.time()
    raw = Path(a.image).read_bytes()
    img = load_png(raw)
    flags, seen = [], set()

    def add(flag, where, spec=None, key=None):
        if flag not in seen:
            seen.add(flag)
            flags.append({"flag": flag, "where": where, "spec": spec, "key": key})

    iend = raw.find(b"IEND")
    for m in RAW_FLAG.finditer(raw):
        where = "after IEND (trailer) - plain bytes, likely a decoy" if iend != -1 and m.start() > iend else "plain bytes / metadata chunk - likely a decoy"
        add(m.group(0).decode(), where)
    meta = {k: str(v) for k, v in (img.info or {}).items() if isinstance(v, (str, bytes))}
    signals = [] if a.quick else lsb_signals(img)
    techniques = [t for t in a.only.split(",") if t] or None
    hit = lambda f, spec, key: add(f, f"EXTRACTED via {spec['technique']}" + (f" with key {key!r}" if key else ""), spec, key)  # noqa: E731

    if a.quick:
        for spec in candidate_specs(img, keyed=False):
            if (f := extract(img, spec, None)):
                hit(f, spec.to_dict(), None)
        stats = None
    else:
        keys: list[str | None] = [None]
        keys += [v for v in meta.values() if v and not FLAG_RE.search(v) and not v.startswith(MSG_PREFIX)]
        keys += [k for k in a.keys.split(",") if k]
        if a.wordlist:
            keys += wordlist()
        if a.biglist:
            keys += bigwordlist()
        keys = list(dict.fromkeys(keys))
        narrow = dict(channels=[c for c in a.channels.split(",") if c] or None,
                      bit_planes=[int(b) for b in a.bit_planes.split(",") if b] or None,
                      deltas=[int(d) for d in a.deltas.split(",") if d] or None,
                      coeffs=[c for c in a.coeffs.split(";") if c] or None)
        stats = search(img, keys, techniques=techniques, budget_s=a.budget, on_hit=hit, **narrow)
        if not stats["complete"]:
            signals.append({"anomaly": f"budget hit after {stats['keys_tried']}/{stats['keys_total']} keys - narrow with --only or raise --budget", "technique": None})
    out = {"flags": flags, "signals": signals, "metadata": meta, "search": stats, "elapsed_s": round(time.time() - t0, 2)}
    json.dump(out, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
