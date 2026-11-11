"""Top-level embed / extract / serialise for a scheme spec."""

from __future__ import annotations

import io
import re

from PIL import Image, PngImagePlugin

from .bits import decrypt, encrypt, frame, unframe
from .spec import TECHNIQUES, SchemeSpec, validate_spec
from .techniques import TECHNIQUE_IMPLS

FLAG_RE = re.compile(r"FLAG\{[A-Za-z0-9_]{4,64}\}")
META_KEY_FIELD = "Comment"  # PNG tEXt chunk that carries the key when key_source=metadata
DECOY_META_FIELD = "Description"

__all__ = ["TECHNIQUES", "FLAG_RE", "embed", "extract", "save_png", "load_png", "difficulty"]


def embed(cover: Image.Image, flag: str, spec: SchemeSpec | dict, key: str | None = None) -> Image.Image:
    spec = validate_spec(spec)
    impl = TECHNIQUE_IMPLS[spec.technique]
    carrier = impl.prepare(cover, spec)
    payload = encrypt(spec.cipher, key, flag.encode())
    return impl.embed_bits(carrier, frame(payload), spec, key)


def extract(img: Image.Image, spec: SchemeSpec | dict, key: str | None = None) -> str | None:
    """Return the hidden flag if `spec`+`key` are right, else None. Never raises on bad guesses."""
    try:
        spec = validate_spec(spec)
        bits = TECHNIQUE_IMPLS[spec.technique].carrier_bits(img, spec, key)
        payload = unframe(bits)
        if payload is None:
            return None
        text = decrypt(spec.cipher, key, payload).decode("utf-8", errors="replace")
    except Exception:
        return None
    m = FLAG_RE.fullmatch(text)
    return m.group(0) if m else None


def save_png(img: Image.Image, spec: SchemeSpec | dict, key: str | None, decoy_flag: str | None = None) -> bytes:
    """Serialise to PNG, placing the key (key_source=metadata) and decoys."""
    spec = validate_spec(spec)
    info = PngImagePlugin.PngInfo()
    if spec.key_source == "metadata" and key:
        info.add_text(META_KEY_FIELD, key)
    if spec.decoy == "metadata_text" and decoy_flag:
        info.add_text(DECOY_META_FIELD, decoy_flag)
    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=info, optimize=False)
    data = buf.getvalue()
    if spec.decoy == "trailer" and decoy_flag:
        data += b"\n" + decoy_flag.encode() + b"\n"  # bytes after IEND
    return data


def load_png(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def difficulty(spec: SchemeSpec | dict) -> float:
    """Heuristic 1..10 difficulty used to seed Elo and sort the ladder."""
    s = validate_spec(spec)
    d = {"lsb": 1.0, "alpha": 1.5, "palette": 2.0, "dct": 3.0}[s.technique]
    if s.technique == "lsb":
        d += 0.5 * s.bit_plane + (0.5 if len(s.channels) < 3 else 0)
    if s.technique == "alpha" and s.alpha_mode == "transparent_rgb":
        d -= 0.5
    d += {"sequential": 0.0, "prng": 1.5}[s.order]
    d += {"none": 0.0, "xor": 1.0, "aes": 1.5}[s.cipher]
    d += {"none": 0.0, "metadata": 0.5, "wordlist": 1.5, "biglist": 3.0, "secret": 4.0}[s.key_source]
    d += {"none": 0.0, "metadata_text": 0.5, "trailer": 0.5, "pixel": 1.0}[s.decoy]
    return round(min(d, 10.0), 2)
