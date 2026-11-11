import itertools

import pytest

from stegoarena.arena_schemes import SpecError, difficulty, embed, extract, load_png, save_png, validate_spec
from stegoarena.generator.covers import make_cover

FLAG = "FLAG{round_trip_ok_42}"
COVER = make_cover(seed=7, size=256)

CASES = [
    dict(technique="lsb"),
    dict(technique="lsb", channels=["B"], bit_plane=2),
    dict(technique="lsb", channels=["R", "G"], bit_plane=1, order="prng", key_source="secret"),
    dict(technique="dct", channels=["G"], dct_coeff="2,1", dct_delta=12),
    dict(technique="dct", channels=["R"], dct_coeff="3,2", dct_delta=20, order="prng", key_source="wordlist"),
    dict(technique="palette", palette_colors=64),
    dict(technique="palette", palette_colors=17, order="prng", key_source="metadata"),
    dict(technique="alpha", alpha_mode="alpha_lsb"),
    dict(technique="alpha", alpha_mode="transparent_rgb", order="prng", key_source="secret"),
]
CIPHERS = ["none", "xor", "aes"]


@pytest.mark.parametrize("case,cipher", list(itertools.product(CASES, CIPHERS)))
def test_round_trip_through_png(case, cipher):
    spec = dict(case, cipher=cipher)
    if cipher != "none" and spec.get("key_source", "none") == "none":
        spec["key_source"] = "metadata"
    spec = validate_spec(spec)
    key = None if spec.key_source == "none" else "dragon"
    img = embed(COVER, FLAG, spec, key)
    data = save_png(img, spec, key, decoy_flag="FLAG{decoy_not_real}")
    back = load_png(data)
    assert extract(back, spec, key) == FLAG
    if key:
        assert extract(back, spec, "wrongkey") != FLAG


def test_decoys_and_metadata_key_present():
    spec = validate_spec(dict(technique="lsb", cipher="xor", key_source="metadata", decoy="trailer"))
    data = save_png(embed(COVER, FLAG, spec, "tiger"), spec, "tiger", decoy_flag="FLAG{decoy_trailer}")
    assert data.rstrip().endswith(b"FLAG{decoy_trailer}")
    assert load_png(data).info.get("Comment") == "tiger"


def test_invalid_specs_rejected():
    with pytest.raises(SpecError):
        validate_spec(dict(technique="lsb", order="prng", key_source="none"))
    with pytest.raises(SpecError):
        validate_spec(dict(technique="wavelet"))


def test_difficulty_monotone():
    easy = difficulty(dict(technique="lsb"))
    hard = difficulty(dict(technique="dct", order="prng", cipher="aes", key_source="secret"))
    assert easy < hard


@pytest.mark.parametrize("case", [
    dict(technique="lsb", channels=["R", "B"], bit_plane=1, order="prng", cipher="aes", key_source="wordlist"),
    dict(technique="dct", channels=["G"], dct_coeff="2,3", dct_delta=20, order="prng", cipher="xor", key_source="wordlist"),
    dict(technique="alpha", alpha_mode="transparent_rgb", order="prng", cipher="xor", key_source="wordlist"),
    dict(technique="palette", palette_colors=64, cipher="aes", key_source="wordlist"),
])
def test_bruteforce_recovers_wordlist_keys(case):
    from pathlib import Path

    from stegoarena.arena_schemes import bruteforce

    words = (Path(bruteforce.__file__).parent / "wordlist.txt").read_text().split()
    spec = validate_spec(case)
    back = load_png(save_png(embed(COVER, FLAG, spec, words[-3]), spec, words[-3]))
    found = []
    stats = bruteforce.search(back, [None] + words, budget_s=60, on_hit=lambda f, s, k: found.append((f, k)))
    assert stats["complete"] and (FLAG, words[-3]) in found
