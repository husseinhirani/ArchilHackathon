"""Pixel decoys: a second, fake flag embedded with a real scheme.

Metadata/trailer decoys sit in plain bytes, so "only trust extracted flags" beats
them. A pixel decoy is recovered by `extract()` exactly like the real flag, so
extraction alone no longer proves a flag is genuine.

The decoy is always LSB, on a carrier disjoint from the real scheme's, and it
mirrors the real scheme's order/cipher/key so it is no easier or harder to reach:
    lsb     -> same channels pool, a different bit plane
    dct     -> plane 0 of channel(s) other than the DCT channel
    alpha   -> plane 0 of RGB (alpha_lsb never touches RGB; transparent_rgb only
               overwrites the bottom rows, so the decoy is kept sequential/top rows)
    palette -> unsupported (index LSBs are the only carrier); rejected by validate_spec
"""

from __future__ import annotations

import random

from .spec import CHANNELS, SchemeSpec, validate_spec

CHANNEL_SETS = [["R", "G", "B"], ["R"], ["G"], ["B"], ["R", "G"], ["R", "B"], ["G", "B"]]


def pixel_decoy_spec(spec: SchemeSpec | dict, rng: random.Random) -> SchemeSpec:
    spec = validate_spec(spec)
    order = spec.order
    if spec.technique == "lsb":
        channels = rng.choice(CHANNEL_SETS)
        plane = rng.choice([p for p in (0, 1, 2) if p != spec.bit_plane])
    elif spec.technique == "dct":
        others = [c for c in CHANNELS if c != spec.channels[0]]
        channels = rng.choice([others, others[:1], others[1:]])
        plane = 0
    elif spec.technique == "alpha":
        channels = rng.choice(CHANNEL_SETS)
        plane = 0
        if spec.alpha_mode == "transparent_rgb":
            order = "sequential"  # prng could scatter decoy bits into the transparent strip
    else:
        raise ValueError(f"pixel decoy not supported for technique {spec.technique!r}")
    # validate_spec drops key_source to "none" if the decoy ends up unkeyed.
    return validate_spec(SchemeSpec(technique="lsb", channels=list(channels), bit_plane=plane, order=order,
                                    cipher=spec.cipher, key_source=spec.key_source))
