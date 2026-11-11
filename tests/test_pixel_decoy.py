"""Pixel decoys: a second fake flag that extract() recovers just like the real one."""

import itertools
import json
import random

import pytest

from stegoarena.arena_schemes import SpecError, extract, load_png, validate_spec
from stegoarena.arena_schemes.decoys import pixel_decoy_spec
from stegoarena.generator.build import build_scheme
from stegoarena.hider.ladder import LADDER
from stegoarena.store import LocalStore

CASES = [
    dict(technique="lsb"),
    dict(technique="lsb", channels=["B"], bit_plane=1),
    dict(technique="lsb", channels=["R", "G"], bit_plane=2, order="prng", key_source="metadata"),
    dict(technique="dct", channels=["G"], dct_coeff="2,1", dct_delta=12),
    dict(technique="dct", channels=["R"], dct_coeff="3,2", dct_delta=20, order="prng", key_source="wordlist"),
    dict(technique="alpha", alpha_mode="alpha_lsb"),
    dict(technique="alpha", alpha_mode="alpha_lsb", order="prng", key_source="metadata"),
    dict(technique="alpha", alpha_mode="transparent_rgb", order="prng", key_source="biglist"),
]


@pytest.mark.parametrize("case,cipher", list(itertools.product(CASES, ["none", "xor", "aes"])))
def test_both_flags_extract_from_built_image(tmp_path, case, cipher):
    spec = dict(case, cipher=cipher, decoy="pixel")
    if cipher != "none" and spec.get("key_source", "none") == "none":
        spec["key_source"] = "metadata"
    store = LocalStore(tmp_path)
    b = build_scheme(store, 1, "s", validate_spec(spec), seed=3)
    truth = json.loads((tmp_path / "rounds/1/schemes/s/truth.json").read_text())
    img = load_png((tmp_path / b.image_key).read_bytes())
    dspec = truth["decoy_spec"]
    assert extract(img, b.spec, b.key) == b.flag
    assert extract(img, dspec, b.key) == b.decoy_flag != b.flag
    # The decoy mirrors the real scheme's keying, so it is no easier to reach.
    assert (dspec["order"], dspec["cipher"]) == (
        "sequential" if b.spec.alpha_mode == "transparent_rgb" and b.spec.technique == "alpha" else b.spec.order,
        b.spec.cipher,
    )


@pytest.mark.parametrize("case", CASES)
def test_decoy_carrier_is_disjoint(case):
    spec = validate_spec(case)
    for seed in range(20):
        d = pixel_decoy_spec(spec, random.Random(seed))
        assert d.technique == "lsb"
        if spec.technique == "lsb":
            assert d.bit_plane != spec.bit_plane
        if spec.technique == "dct":
            assert spec.channels[0] not in d.channels


def test_pixel_decoy_rejected_for_palette():
    with pytest.raises(SpecError):
        validate_spec(dict(technique="palette", decoy="pixel"))


def test_ladder_pixel_rungs_build(tmp_path):
    store = LocalStore(tmp_path)
    rungs = [r for r in LADDER if r.get("decoy") == "pixel"]
    assert rungs
    for i, r in enumerate(rungs):
        build_scheme(store, 1, f"p{i}", validate_spec(dict(r)), seed=10 + i)
