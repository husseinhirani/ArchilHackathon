"""Scheme spec: a flat dict, validated here (no pydantic so it runs in sandboxes).

Fields (all required after normalisation):
    technique     lsb | dct | palette | alpha
    channels      subset of ["R","G","B"]          (lsb; dct uses channels[0])
    bit_plane     0..2                               (lsb)
    order         sequential | prng                  (pixel/block visiting order)
    dct_coeff     "u,v" mid-frequency position       (dct)
    dct_delta     12..32 QIM step                      (dct)
    palette_colors 16..256, even                      (palette)
    alpha_mode    alpha_lsb | transparent_rgb         (alpha)
    cipher        none | xor | aes                    (payload encryption)
    key_source    none | metadata | wordlist | biglist | secret (where the key lives;
                  biglist = word+NN from wordlist.txt x 00..99, ~16k keys; secret is banned in the arena)
    decoy         none | metadata_text | trailer | pixel  (fake flag; pixel = second LSB payload, see decoys.py)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

TECHNIQUES = ("lsb", "dct", "palette", "alpha")
CHANNELS = ("R", "G", "B")
ORDERS = ("sequential", "prng")
DCT_COEFFS = ("1,2", "2,1", "2,2", "3,1", "1,3", "3,2", "2,3")
ALPHA_MODES = ("alpha_lsb", "transparent_rgb")
CIPHERS = ("none", "xor", "aes")
KEY_SOURCES = ("none", "metadata", "wordlist", "biglist", "secret")
DECOYS = ("none", "metadata_text", "trailer", "pixel")


class SpecError(ValueError):
    pass


@dataclass
class SchemeSpec:
    technique: str = "lsb"
    channels: list[str] = field(default_factory=lambda: ["R", "G", "B"])
    bit_plane: int = 0
    order: str = "sequential"
    dct_coeff: str = "2,1"
    dct_delta: int = 12
    palette_colors: int = 64
    alpha_mode: str = "alpha_lsb"
    cipher: str = "none"
    key_source: str = "none"
    decoy: str = "none"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SchemeSpec":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return validate_spec(cls(**known))


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise SpecError(msg)


def validate_spec(spec: SchemeSpec | dict, *, allow_secret: bool = True) -> SchemeSpec:
    if isinstance(spec, dict):
        spec = SchemeSpec(**{k: v for k, v in spec.items() if k in SchemeSpec.__dataclass_fields__})
    _need(spec.technique in TECHNIQUES, f"technique must be one of {TECHNIQUES}")
    chans = [c.upper() for c in spec.channels]
    _need(len(chans) >= 1 and all(c in CHANNELS for c in chans), "channels must be non-empty subset of R,G,B")
    spec.channels = sorted(set(chans), key=CHANNELS.index)
    _need(isinstance(spec.bit_plane, int) and 0 <= spec.bit_plane <= 2, "bit_plane must be 0..2")
    _need(spec.order in ORDERS, f"order must be one of {ORDERS}")
    _need(spec.dct_coeff in DCT_COEFFS, f"dct_coeff must be one of {DCT_COEFFS}")
    _need(12 <= int(spec.dct_delta) <= 32, "dct_delta must be 12..32")
    spec.dct_delta = int(spec.dct_delta)
    pc = int(spec.palette_colors)
    _need(16 <= pc <= 256, "palette_colors must be 16..256")
    spec.palette_colors = pc - (pc % 2)
    _need(spec.alpha_mode in ALPHA_MODES, f"alpha_mode must be one of {ALPHA_MODES}")
    _need(spec.cipher in CIPHERS, f"cipher must be one of {CIPHERS}")
    _need(spec.key_source in KEY_SOURCES, f"key_source must be one of {KEY_SOURCES}")
    _need(spec.decoy in DECOYS, f"decoy must be one of {DECOYS}")
    _need(not (spec.decoy == "pixel" and spec.technique == "palette"), "pixel decoy is not supported with palette")
    _need(allow_secret or spec.key_source != "secret",
          "solvability gate: key_source=secret is unrecoverable; use metadata, wordlist or biglist")
    keyed = spec.order == "prng" or spec.cipher != "none"
    _need(not (keyed and spec.key_source == "none"), "prng order or a cipher needs key_source != none")
    if not keyed:
        spec.key_source = "none"
    return spec
