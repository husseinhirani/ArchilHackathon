"""Fast key/parameter search over every carrier the scheme family can produce.

Speed-ups versus calling extract() per (spec, key):
  * each carrier's bits are computed once in natural order (key-independent);
  * keyed permutations are cached per (carrier size, key) - many specs share a size;
  * only the 16-bit length header is decoded first; implausible lengths are skipped;
  * all three ciphers are tried on one decoded payload.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np

from .api import FLAG_RE
from .bits import LEN_BITS, decrypt, visit_order
from .spec import CHANNELS, CIPHERS, DCT_COEFFS, SchemeSpec
from .techniques import _D, _blocks

MAX_PAYLOAD = 128  # bytes; real flags are ~22
_HERE = __import__("pathlib").Path(__file__).parent


def wordlist() -> list[str]:
    return (_HERE / "wordlist.txt").read_text().split()


def bigwordlist() -> list[str]:
    """~16k keys: every wordlist word with a two-digit suffix 00..99. Too many to try
    against every carrier in 90s; fine against a narrowed carrier set."""
    return [f"{w}{i:02d}" for w in wordlist() for i in range(100)]


@dataclass
class Carrier:
    spec: dict  # base spec (order/cipher/key_source filled per hit)
    n: int  # number of carrier units (bits, or bytes when bytewise)
    bits: np.ndarray | None = None  # natural-order bit per unit
    data: np.ndarray | None = None  # natural-order bytes (transparent_rgb)

    def decode(self, perm: np.ndarray) -> bytes | None:
        if self.data is not None:  # bytewise carrier: header = first 2 bytes in visit order
            if self.n < 2:
                return None
            ln = int(self.data[perm[0]]) << 8 | int(self.data[perm[1]])
            if not 1 <= ln <= MAX_PAYLOAD or 2 + ln > self.n:
                return None
            return self.data[perm[2 : 2 + ln]].tobytes()
        head = self.bits[perm[:LEN_BITS]]
        ln = int(np.packbits(head).view(">u2")[0])
        if not 1 <= ln <= MAX_PAYLOAD or LEN_BITS + 8 * ln > self.n:
            return None
        return np.packbits(self.bits[perm[LEN_BITS : LEN_BITS + 8 * ln]]).tobytes()


def build_carriers(img, techniques: Iterable[str] | None = None, *, channels: Iterable[str] | None = None,
                   bit_planes: Iterable[int] | None = None, deltas: Iterable[int] | None = None,
                   coeffs: Iterable[str] | None = None) -> list[Carrier]:
    """All carriers the family can produce, optionally narrowed (channels match exactly for lsb,
    channels[0] for dct)."""
    techniques = set(techniques or ("lsb", "dct", "palette", "alpha"))
    want_ch = {"".join(sorted(c.upper(), key="RGB".index)) for c in channels} if channels else None
    bit_planes = set(bit_planes) if bit_planes is not None else {0, 1, 2}
    deltas = set(deltas) if deltas else set(range(12, 33, 2))
    coeffs = set(coeffs) if coeffs else set(DCT_COEFFS)
    out: list[Carrier] = []
    if img.mode == "P":
        if "palette" in techniques:
            idx = np.array(img, dtype=np.uint8).reshape(-1)
            out.append(Carrier({"technique": "palette"}, idx.size, bits=idx & 1))
        return out
    rgba = np.array(img.convert("RGBA"), dtype=np.uint8)
    if "lsb" in techniques:
        for chans in (["R", "G", "B"], ["R"], ["G"], ["B"], ["R", "G"], ["R", "B"], ["G", "B"]):
            if want_ch and "".join(chans) not in want_ch:
                continue
            flat = rgba[:, :, [CHANNELS.index(c) for c in chans]].reshape(-1)
            for bp in sorted(bit_planes):
                out.append(Carrier({"technique": "lsb", "channels": chans, "bit_plane": bp}, flat.size, bits=(flat >> bp) & 1))
    if "alpha" in techniques and img.mode == "RGBA":
        a = rgba[:, :, 3].reshape(-1)
        out.append(Carrier({"technique": "alpha", "alpha_mode": "alpha_lsb"}, a.size, bits=a & 1))
        rows = np.where((rgba[:, :, 3] == 0).all(axis=1))[0]
        if len(rows):
            region = rgba[rows[0] :, :, :3].reshape(-1)
            out.append(Carrier({"technique": "alpha", "alpha_mode": "transparent_rgb"}, region.size, data=region))
    if "dct" in techniques:
        for ch in CHANNELS:
            if want_ch and not any(ch in w for w in want_ch):
                continue
            cmat = _D @ _blocks(rgba[:, :, CHANNELS.index(ch)]) @ _D.T
            for cs in DCT_COEFFS:
                if cs not in coeffs:
                    continue
                u, v = (int(t) for t in cs.split(","))
                x = cmat[:, u, v]
                for d in sorted(deltas):
                    e0 = np.abs(x - d * np.round(x / d))
                    e1 = np.abs(x - (d * np.round((x - d / 2) / d) + d / 2))
                    out.append(Carrier({"technique": "dct", "channels": [ch], "dct_coeff": cs, "dct_delta": d},
                                       len(x), bits=(e1 < e0).astype(np.uint8)))
    return out


def search(img, keys: list[str | None], *, techniques=None, budget_s: float = 30.0,
           on_hit: Callable[[str, dict, str | None], None] | None = None, **narrow) -> dict:
    """Try every carrier x {sequential, prng} x key x cipher. Returns stats; hits via on_hit.
    `narrow` = channels / bit_planes / deltas / coeffs passed to build_carriers."""
    t0 = time.time()
    carriers = build_carriers(img, techniques, **narrow)
    perm_cache: dict[tuple[int, str, str | None], np.ndarray] = {}
    tried, hits, done_keys = 0, 0, 0
    for key in keys:
        if time.time() - t0 > budget_s:
            break
        for order in ("sequential", "prng"):
            if order == "prng" and key is None:
                continue
            for c in carriers:
                pk = (c.n, order, key if order == "prng" else None)
                if pk not in perm_cache:
                    perm_cache[pk] = visit_order(c.n, order, key)
                payload = c.decode(perm_cache[pk])
                tried += 1
                if payload is None:
                    continue
                for cipher in (("none",) if key is None else CIPHERS):
                    if key is not None and order == "sequential" and cipher == "none":
                        continue  # identical to the unkeyed pass
                    try:
                        text = decrypt(cipher, key, payload).decode("utf-8")
                    except Exception:
                        continue
                    if FLAG_RE.fullmatch(text):
                        hits += 1
                        spec = SchemeSpec(**{**c.spec, "order": order, "cipher": cipher,
                                             "key_source": "none" if key is None else "keyed"}).to_dict()
                        if on_hit:
                            on_hit(text, spec, key)
        # drop big keyed permutations once a key is done (memory)
        perm_cache = {k: v for k, v in perm_cache.items() if k[1] == "sequential"}
        done_keys += 1
    return {"carriers": len(carriers), "decodes_tried": tried, "keys_tried": done_keys, "keys_total": len(keys),
            "hits": hits, "elapsed_s": round(time.time() - t0, 2), "complete": done_keys == len(keys)}
