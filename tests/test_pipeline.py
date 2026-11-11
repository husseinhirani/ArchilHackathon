"""Provider, scorer, Elo, generator and end-to-end smoke tests. No network, no API keys."""

import asyncio
import json

import pytest

from stegoarena.arena_schemes import validate_spec
from stegoarena.config import Config
from stegoarena.generator.build import build_scheme
from stegoarena.hider.ladder import KNOWN_EASY, LADDER, ladder_specs, mutate
from stegoarena.orchestrator import Arena
from stegoarena.sandbox.local_provider import LocalProvider
from stegoarena.scorer import EloBook, score_attempt
from stegoarena.store import LocalStore


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ARCHIL_API_KEY", "ARCHIL_DISK_ID"):
        monkeypatch.delenv(k, raising=False)
    c = Config()
    c.arena_root, c.cache_dir, c.provider, c.keenable_enabled = tmp_path / "arena", tmp_path / "cache", "local", False
    c.anthropic_api_key = c.archil_api_key = c.archil_disk_id = None
    c.cache_dir.mkdir()
    return c


def test_local_provider_fork_exec_files(cfg):
    async def go():
        p = LocalProvider(cfg)
        t = await p.create_from_template()
        a, b = await p.fork(t), await p.fork(t)
        await p.write_file(a, "x.txt", b"hello")
        assert (await p.exec(a, "cat x.txt")).stdout == "hello"
        assert (await p.exec(b, "test -f x.txt")).exit_code != 0  # forks are isolated
        r = await p.exec(a, "python3 -c 'import arena_schemes; print(1)'")
        assert r.stdout.strip() == "1", r.stderr
        assert (await p.exec(a, "sleep 5", timeout_s=0.5)).timed_out
        await p.destroy(a)
        await p.destroy(b)

    asyncio.run(go())


def test_generator_writes_layout(tmp_path):
    st = LocalStore(tmp_path)
    b = build_scheme(st, 3, "r3s0", KNOWN_EASY, seed=1)
    for f in ("image.png", "spec.json", "truth.json"):
        assert st.exists(f"rounds/3/schemes/r3s0/{f}")
    assert st.read_json("rounds/3/schemes/r3s0/truth.json")["flag"] == b.flag


def test_scorer_partial_credit_and_decoy():
    spec = validate_spec(dict(technique="lsb", channels=["R"], bit_plane=1)).to_dict()
    truth = {"flag": "FLAG{real_one}", "decoy_flag": "FLAG{decoy_one}"}
    full = score_attempt({"submitted_flag": "FLAG{real_one}", "signals": []}, spec, truth)
    assert full["cracked"] and full["score"] >= 0.9
    partial = score_attempt({"submitted_flag": None, "signals": [{"anomaly": "LSB oddity in red", "technique": "lsb",
                                                                    "params": {"channels": ["R"], "bit_plane": 1}}]}, spec, truth)
    assert 0.3 < partial["score"] < 0.9
    decoy = score_attempt({"submitted_flag": "FLAG{decoy_one}", "signals": []}, spec, truth)
    assert decoy["decoy_submitted"] and decoy["score"] == 0.0


def test_elo_moves_in_right_direction(tmp_path):
    book = EloBook(LocalStore(tmp_path))
    e1 = book.update(round_no=1, finder="f", scheme="s1", difficulty=5, score=1.0)
    e2 = book.update(round_no=1, finder="f", scheme="s2", difficulty=5, score=0.0)
    assert e1["finder_rating"] > 1200 and e2["scheme_rating"] > 1200
    book.save()
    assert json.loads((tmp_path / "elo.json").read_text())["history"][0]["scheme"] == "s1"


def test_ladder_and_mutation_valid():
    import random

    for d in LADDER:
        validate_spec(dict(d))
    rng = random.Random(0)
    for d in LADDER:
        validate_spec(mutate(dict(d), rng).to_dict())
    assert len(ladder_specs(1, 4, [], [], seed=1)) == 4


def test_smoke_known_easy_scripted_cracks(cfg):
    """If this fails, the bug is in tooling (sandbox/generator/triage), not scheme difficulty."""

    async def go():
        arena = Arena(cfg, finder="scripted", hider="ladder")
        s = build_scheme(arena.store, 0, "smoke", KNOWN_EASY, seed=5)
        r = await arena.attempt(0, s, 0, "scripted")
        await arena.close()
        return r

    for _ in range(3):
        assert asyncio.run(go())["cracked"]


def test_round_runs_offline(cfg):
    async def go():
        arena = Arena(cfg, finder="llm", hider="llm")  # no key -> falls back to scripted + ladder
        out = await arena.run_round(1, 3, 2)
        await arena.close()
        return arena, out

    arena, out = asyncio.run(go())
    assert arena.finder_kind == "scripted" and len(out["schemes"]) == 3
    assert arena.store.exists("elo.json") and arena.store.exists("lineage.json")
    att = out["schemes"][0]["attempts"][0]
    assert arena.store.exists(f"rounds/1/attempts/{att}/trace.jsonl")
