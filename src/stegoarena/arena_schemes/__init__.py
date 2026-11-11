"""Deterministic image-steganography scheme library.

Kerckhoffs's principle: this package is copied verbatim into every finder
sandbox ($ARENA_TOOLS/arena_schemes). The finder knows the *algorithms*; it does
not know the spec (technique, params) or the key. Keep this package
self-contained: relative imports only, deps = numpy, Pillow, cryptography.
"""

from .api import FLAG_RE, TECHNIQUES, difficulty, embed, extract, load_png, save_png
from .spec import SchemeSpec, SpecError, validate_spec

__all__ = [
    "FLAG_RE", "TECHNIQUES", "SchemeSpec", "SpecError", "difficulty", "embed",
    "extract", "load_png", "save_png", "validate_spec",
]
