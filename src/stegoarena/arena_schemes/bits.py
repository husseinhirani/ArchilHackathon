"""Bit packing, framing, keyed ordering and payload ciphers."""

from __future__ import annotations

import hashlib

import numpy as np

LEN_BITS = 16  # payload framed as 2-byte big-endian length + bytes


def to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def from_bits(bits: np.ndarray) -> bytes:
    return np.packbits(bits.astype(np.uint8)).tobytes()


def frame(payload: bytes) -> np.ndarray:
    if len(payload) >= 1 << LEN_BITS:
        raise ValueError("payload too large")
    return to_bits(len(payload).to_bytes(2, "big") + payload)


def unframe(bits: np.ndarray) -> bytes | None:
    """Parse a framed payload from a carrier bitstream (may be longer)."""
    if len(bits) < LEN_BITS:
        return None
    n = int.from_bytes(from_bits(bits[:LEN_BITS]), "big")
    if n == 0 or LEN_BITS + 8 * n > len(bits):
        return None
    return from_bits(bits[LEN_BITS : LEN_BITS + 8 * n])


def seed_from_key(key: str | None) -> int:
    return int.from_bytes(hashlib.sha256((key or "").encode()).digest()[:8], "big")


def visit_order(n: int, order: str, key: str | None) -> np.ndarray:
    """Positions 0..n-1 in sequential order, or a keyed permutation."""
    if order == "sequential":
        return np.arange(n)
    return np.random.default_rng(seed_from_key(key)).permutation(n)


# ---- ciphers (deterministic from key; the finder has this code too) ----

def _xor_stream(key: str, n: int) -> bytes:
    out = b""
    ctr = 0
    while len(out) < n:
        out += hashlib.sha256(key.encode() + ctr.to_bytes(4, "big")).digest()
        ctr += 1
    return out[:n]


def _aes(key: str, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    k = hashlib.sha256(key.encode()).digest()
    nonce = hashlib.sha256(b"nonce:" + key.encode()).digest()[:16]
    enc = Cipher(algorithms.AES(k), modes.CTR(nonce)).encryptor()
    return enc.update(data) + enc.finalize()


def encrypt(cipher: str, key: str | None, data: bytes) -> bytes:
    if cipher == "none":
        return data
    if key is None:
        raise ValueError("cipher needs a key")
    if cipher == "xor":
        return bytes(a ^ b for a, b in zip(data, _xor_stream(key, len(data))))
    if cipher == "aes":
        return _aes(key, data)  # CTR mode: encrypt == decrypt
    raise ValueError(cipher)


decrypt = encrypt  # both ciphers are symmetric stream ciphers
