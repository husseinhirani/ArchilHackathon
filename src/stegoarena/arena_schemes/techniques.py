"""Carrier techniques. Each exposes:

    prepare(cover: Image, spec) -> Image          # convert cover into carrier format
    embed_bits(img, bits, spec, key) -> Image
    carrier_bits(img, spec, key) -> np.ndarray    # every carrier bit in visit order
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from .bits import visit_order
from .spec import CHANNELS, SchemeSpec


def _chan_idx(spec: SchemeSpec) -> list[int]:
    return [CHANNELS.index(c) for c in spec.channels]


def _check_cap(n_bits: int, cap: int) -> None:
    if n_bits > cap:
        raise ValueError(f"payload {n_bits} bits exceeds capacity {cap}")


# --------------------------------------------------------------------- LSB
class LSB:
    @staticmethod
    def prepare(cover: Image.Image, spec: SchemeSpec) -> Image.Image:
        return cover.convert("RGB")

    @staticmethod
    def embed_bits(img: Image.Image, bits: np.ndarray, spec: SchemeSpec, key: str | None) -> Image.Image:
        arr = np.array(img.convert("RGB"), dtype=np.uint8)
        ch = _chan_idx(spec)
        carrier = arr[:, :, ch].reshape(-1)  # pixel-major, selected channels interleaved
        _check_cap(len(bits), carrier.size)
        pos = visit_order(carrier.size, spec.order, key)[: len(bits)]
        mask = np.uint8(1 << spec.bit_plane)
        carrier[pos] = (carrier[pos] & ~mask) | (bits.astype(np.uint8) << spec.bit_plane)
        arr[:, :, ch] = carrier.reshape(arr.shape[0], arr.shape[1], len(ch))
        return Image.fromarray(arr, "RGB")

    @staticmethod
    def carrier_bits(img: Image.Image, spec: SchemeSpec, key: str | None) -> np.ndarray:
        arr = np.array(img.convert("RGB"), dtype=np.uint8)
        carrier = arr[:, :, _chan_idx(spec)].reshape(-1)
        pos = visit_order(carrier.size, spec.order, key)
        return (carrier[pos] >> spec.bit_plane) & 1


# --------------------------------------------------------------------- DCT
_N = 8
_D = np.array(
    [[(np.sqrt(1 / _N) if k == 0 else np.sqrt(2 / _N)) * np.cos(np.pi * (2 * n + 1) * k / (2 * _N)) for n in range(_N)] for k in range(_N)]
)  # orthonormal DCT-II matrix: C = D @ B @ D.T


def _blocks(plane: np.ndarray) -> np.ndarray:
    h, w = (plane.shape[0] // _N) * _N, (plane.shape[1] // _N) * _N
    p = plane[:h, :w].astype(np.float64)
    return p.reshape(h // _N, _N, w // _N, _N).swapaxes(1, 2).reshape(-1, _N, _N)


def _unblocks(blocks: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    hb, wb = shape[0] // _N, shape[1] // _N
    return blocks.reshape(hb, wb, _N, _N).swapaxes(1, 2).reshape(hb * _N, wb * _N)


class DCT:
    """QIM on one mid-frequency 8x8 block-DCT coefficient of one colour plane.
    One bit per block. Plane is clamped to [16, 239] so the inverse never clips."""

    @staticmethod
    def _uv(spec: SchemeSpec) -> tuple[int, int]:
        u, v = (int(x) for x in spec.dct_coeff.split(","))
        return u, v

    @staticmethod
    def prepare(cover: Image.Image, spec: SchemeSpec) -> Image.Image:
        arr = np.array(cover.convert("RGB"), dtype=np.uint8)
        c = CHANNELS.index(spec.channels[0])
        arr[:, :, c] = np.clip(arr[:, :, c], 16, 239)
        return Image.fromarray(arr, "RGB")

    @staticmethod
    def embed_bits(img: Image.Image, bits: np.ndarray, spec: SchemeSpec, key: str | None) -> Image.Image:
        arr = np.array(img.convert("RGB"), dtype=np.uint8)
        c = CHANNELS.index(spec.channels[0])
        plane = arr[:, :, c]
        blocks = _blocks(plane)
        _check_cap(len(bits), len(blocks))
        u, v = DCT._uv(spec)
        d = float(spec.dct_delta)
        coeffs = _D @ blocks @ _D.T
        pos = visit_order(len(blocks), spec.order, key)[: len(bits)]
        x = coeffs[pos, u, v]
        off = bits.astype(np.float64) * d / 2
        coeffs[pos, u, v] = d * np.round((x - off) / d) + off
        new = _D.T @ coeffs @ _D
        h, w = (plane.shape[0] // _N) * _N, (plane.shape[1] // _N) * _N
        plane[:h, :w] = np.clip(np.round(_unblocks(new, (h, w))), 0, 255).astype(np.uint8)
        arr[:, :, c] = plane
        return Image.fromarray(arr, "RGB")

    @staticmethod
    def carrier_bits(img: Image.Image, spec: SchemeSpec, key: str | None) -> np.ndarray:
        arr = np.array(img.convert("RGB"), dtype=np.uint8)
        blocks = _blocks(arr[:, :, CHANNELS.index(spec.channels[0])])
        u, v = DCT._uv(spec)
        d = float(spec.dct_delta)
        x = (_D @ blocks @ _D.T)[:, u, v]
        pos = visit_order(len(blocks), spec.order, key)
        x = x[pos]
        e0 = np.abs(x - d * np.round(x / d))
        e1 = np.abs(x - (d * np.round((x - d / 2) / d) + d / 2))
        return (e1 < e0).astype(np.uint8)


# ----------------------------------------------------------------- palette
class Palette:
    """Quantise to N colours, sort the palette by luminance so index^1 is a
    near-identical colour, then hide bits in the index LSBs (EzStego-style)."""

    @staticmethod
    def prepare(cover: Image.Image, spec: SchemeSpec) -> Image.Image:
        n = spec.palette_colors
        q = cover.convert("RGB").quantize(colors=n, method=Image.Quantize.MEDIANCUT)
        pal = np.array(q.getpalette()[: 3 * n], dtype=np.int64).reshape(-1, 3)
        if len(pal) < n:  # pad if quantiser returned fewer colours
            pal = np.vstack([pal, np.repeat(pal[-1:], n - len(pal), axis=0)])
        lum = pal @ np.array([299, 587, 114])
        order = np.argsort(lum, kind="stable")
        inv = np.empty_like(order)
        inv[order] = np.arange(len(order))
        idx = inv[np.array(q, dtype=np.int64)].astype(np.uint8)
        out = Image.fromarray(idx, "P")
        out.putpalette(pal[order].astype(np.uint8).reshape(-1).tolist())
        return out

    @staticmethod
    def embed_bits(img: Image.Image, bits: np.ndarray, spec: SchemeSpec, key: str | None) -> Image.Image:
        idx = np.array(img, dtype=np.uint8).reshape(-1)
        _check_cap(len(bits), idx.size)
        pos = visit_order(idx.size, spec.order, key)[: len(bits)]
        idx[pos] = (idx[pos] & 0xFE) | bits.astype(np.uint8)
        out = Image.fromarray(idx.reshape(img.size[1], img.size[0]), "P")
        out.putpalette(img.getpalette())
        return out

    @staticmethod
    def carrier_bits(img: Image.Image, spec: SchemeSpec, key: str | None) -> np.ndarray:
        if img.mode != "P":
            raise ValueError("palette scheme needs a P-mode image")
        idx = np.array(img, dtype=np.uint8).reshape(-1)
        return idx[visit_order(idx.size, spec.order, key)] & 1


# ------------------------------------------------------------------- alpha
class Alpha:
    """alpha_lsb: bits in the alpha channel LSB (alpha 254/255).
    transparent_rgb: bottom rows made fully transparent; raw payload bits stored
    as the full RGB bytes of those invisible pixels."""

    @staticmethod
    def prepare(cover: Image.Image, spec: SchemeSpec) -> Image.Image:
        return cover.convert("RGBA")

    @staticmethod
    def _strip_rows(h: int, w: int, n_bits: int | None) -> int:
        if n_bits is None:
            return 0
        return max(1, -(-n_bits // (w * 3 * 8)))

    @staticmethod
    def embed_bits(img: Image.Image, bits: np.ndarray, spec: SchemeSpec, key: str | None) -> Image.Image:
        arr = np.array(img.convert("RGBA"), dtype=np.uint8)
        h, w = arr.shape[:2]
        if spec.alpha_mode == "alpha_lsb":
            a = arr[:, :, 3].reshape(-1)
            _check_cap(len(bits), a.size)
            pos = visit_order(a.size, spec.order, key)[: len(bits)]
            a[pos] = (a[pos] & 0xFE) | bits.astype(np.uint8)
            arr[:, :, 3] = a.reshape(h, w)
        else:
            rows = Alpha._strip_rows(h, w, len(bits))
            _check_cap(len(bits), h // 4 * w * 24)
            region = arr[h - rows :, :, :3].reshape(-1)
            data = np.packbits(np.concatenate([bits, np.zeros((-len(bits)) % 8, np.uint8)]))
            noise = np.random.default_rng(len(bits)).integers(0, 256, region.size, dtype=np.uint8)
            pos = visit_order(region.size, spec.order, key)
            noise[pos[: len(data)]] = data
            arr[h - rows :, :, :3] = noise.reshape(rows, w, 3)
            arr[h - rows :, :, 3] = 0
        return Image.fromarray(arr, "RGBA")

    @staticmethod
    def carrier_bits(img: Image.Image, spec: SchemeSpec, key: str | None) -> np.ndarray:
        arr = np.array(img.convert("RGBA"), dtype=np.uint8)
        if spec.alpha_mode == "alpha_lsb":
            a = arr[:, :, 3].reshape(-1)
            return a[visit_order(a.size, spec.order, key)] & 1
        transparent_rows = np.where((arr[:, :, 3] == 0).all(axis=1))[0]
        if len(transparent_rows) == 0:
            return np.zeros(0, np.uint8)
        region = arr[transparent_rows[0] :, :, :3].reshape(-1)
        return np.unpackbits(region[visit_order(region.size, spec.order, key)])


TECHNIQUE_IMPLS = {"lsb": LSB, "dct": DCT, "palette": Palette, "alpha": Alpha}
