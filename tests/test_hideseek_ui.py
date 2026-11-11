"""Player-facing decode validation, error wording, log detail and cover grain (Hide & Seek)."""

import numpy as np
import pytest

from stegoarena.generator.covers import make_cover
from stegoarena.hideseek import LEVELS, build_challenge, try_decode
from stegoarena.store import LocalStore

INTERNAL = ("key_source", "prng order or a cipher", "channels must", "SpecError", "alpha_mode", "_")


@pytest.fixture
def medium(tmp_path):
    st = LocalStore(tmp_path)
    cid = build_challenge(st, None, "meet me at noon", "medium")
    return st.read_bytes(f"challenges/{cid}/image.png"), st.read_json(f"challenges/{cid}/truth.json")


def err(data, spec, key=None, wordlist=False):
    r = try_decode(data, spec, key, wordlist)
    assert not r["found"] and "error" in r, r
    assert not any(tok in r["error"] for tok in INTERNAL), f"internal wording leaked: {r['error']!r}"
    return r["error"]


def test_impossible_combos_rejected_with_player_text(medium):
    data, _ = medium  # procedural cover -> RGB image
    assert "no alpha" in err(data, {"technique": "alpha", "channels": ["R", "G", "B"]})
    assert "no alpha" in err(data, {"technique": "transparent", "channels": []})
    assert "palette" in err(data, {"technique": "palette", "channels": []}).lower()
    assert "single colour channel" in err(data, {"technique": "dct", "channels": ["R", "G"]})
    assert "A channel" in err(data, {"technique": "lsb", "channels": ["A"]})
    assert "Bit plane" in err(data, {"technique": "lsb", "channels": ["R"], "bit_plane": 5})


def test_alpha_on_rgba_must_use_A(tmp_path):
    from PIL import Image

    from stegoarena.arena_schemes import embed, save_png, validate_spec

    spec = validate_spec(dict(technique="alpha", alpha_mode="alpha_lsb"))
    data = save_png(embed(Image.new("RGB", (64, 64), (90, 120, 150)), "HS1:alpha secret", spec), spec, None)
    assert "A channel" in err(data, {"technique": "alpha", "channels": ["R", "G", "B"]})
    assert try_decode(data, {"technique": "alpha", "channels": ["A"]}, None)["message"] == "alpha secret"


def test_missing_key_messages(medium):
    data, _ = medium
    assert err(data, {"technique": "lsb", "channels": ["R", "G"], "cipher": "xor"}) == \
        "XOR needs a key. Enter one or click Try every wordlist key."
    assert err(data, {"technique": "lsb", "channels": ["G"], "order": "prng"}) == \
        "PRNG order needs a key (the seed). Enter one or click Try every wordlist key."
    assert "won't change anything" in err(data, {"technique": "lsb", "channels": ["R"]}, wordlist=True)


def test_valid_human_decode_still_works(medium):
    data, truth = medium
    spec = {"technique": "lsb", "channels": ["R", "G"], "bit_plane": 0, "order": "sequential", "cipher": "xor"}
    assert try_decode(data, spec, truth["key"])["message"] == "meet me at noon"
    assert try_decode(data, spec, "  " + truth["key"] + " ")["found"]  # stray spaces from copy-paste


def test_hide_and_seek_cover_has_no_grain_and_arena_cover_unchanged():
    def flips(img):  # how often neighbouring LSBs differ (0.5 = pure noise)
        b = np.array(img)[:, :, 1] & 1
        return float((b[:, 1:] != b[:, :-1]).mean())

    assert flips(make_cover(5, 256)) > 0.45  # arena default keeps sensor noise
    assert flips(make_cover(5, 256, grain=0.0)) < 0.3  # H&S: structure visible in bit planes
    assert np.array_equal(np.array(make_cover(5, 128)), np.array(make_cover(5, 128, grain=4.0)))


def test_levels_unchanged():
    assert set(LEVELS) == {"easy", "medium", "hard"}


def test_bit_plane_clue_matches_level(tmp_path):
    """The UI noise meter compares the first 64 px of row 0 with the rest: sequential levels light
    up exactly the embedding channels; the scrambled hard level shows nothing."""
    from stegoarena.arena_schemes import load_png

    st = LocalStore(tmp_path)

    def start_vs_rest(level):
        cid = build_challenge(st, None, "meet me at the fountain at seven", level)
        a = np.array(load_png(st.read_bytes(f"challenges/{cid}/image.png")).convert("RGB")).astype(int)
        out = {}
        for c, name in enumerate("RGB"):
            p = a[:, :, c] & 1
            out[name] = (float((p[0, 1:64] != p[0, :63]).mean()), float((p[1:, 1:] != p[1:, :-1]).mean()))
        return out

    easy = start_vs_rest("easy")
    assert all(s > r + 0.15 for s, r in easy.values())
    med = start_vs_rest("medium")
    assert med["R"][0] > med["R"][1] + 0.15 and med["G"][0] > med["G"][1] + 0.15
    hard = start_vs_rest("hard")
    assert all(s < 0.3 for s, _ in hard.values())


def test_player_flow_over_http(tmp_path, monkeypatch):
    """Through the real routes: friendly rejections, logged guesses with text, wordlist count."""
    import json
    import time

    from fastapi.testclient import TestClient

    from stegoarena.config import get_config

    monkeypatch.setenv("ARENA_ROOT", str(tmp_path))
    monkeypatch.setenv("STEGOARENA_PROVIDER", "local")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ARCHIL_DISK_ID", "")
    from stegoarena.ui.app import create_app

    with TestClient(create_app(get_config())) as c:
        assert c.get("/api/hs/levels").json()["wordlist_size"] == 157
        cid = c.post("/api/c", json={"message": "the owl flies at dawn", "level": "medium"}).json()["cid"]
        pub = c.get(f"/api/c/{cid}").json()
        assert pub["image_mode"] == "RGB" and pub["wordlist_size"] == 157
        c.post(f"/api/c/{cid}/start")

        def tr(body):
            time.sleep(0.75)  # route rate-limits to ~1 try/s
            return c.post(f"/api/c/{cid}/try", json=body).json()

        r = tr({"spec": {"technique": "alpha", "channels": ["R", "G", "B"]}})
        assert r["error"].startswith("This image has no alpha")
        r = tr({"spec": {"technique": "lsb", "channels": ["R", "G"], "cipher": "xor"}})
        assert r["error"] == "XOR needs a key. Enter one or click Try every wordlist key."
        r = tr({"spec": {"technique": "lsb", "channels": ["R", "G"], "cipher": "xor"}, "wordlist": True})
        assert r["found"] and r["message"] == "the owl flies at dawn"
        g = "a very long wrong guess that keeps going and going past forty characters"
        assert c.post(f"/api/c/{cid}/guess", json={"message": g}).json()["correct"] is False

        human = c.get(f"/api/c/{cid}/state").json()["human"]
        tries = [e for e in human if e["type"] == "try"]
        assert tries[0]["error"].startswith("This image has no alpha") and tries[2]["tried"] >= 1
        guess = next(e for e in human if e["type"] == "guess")
        assert guess["message"] == g and guess["correct"] is False
        assert "key_source" not in json.dumps(human)
