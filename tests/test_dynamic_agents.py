"""Branching, learning, genomes, solvability gate - no network, no API keys."""

import asyncio
import itertools
import json
from types import SimpleNamespace as NS

import pytest

from stegoarena.arena_schemes import SpecError, embed, load_png, save_png, validate_spec
from stegoarena.arena_schemes.bruteforce import bigwordlist, search
from stegoarena.config import Config
from stegoarena.finder import learn
from stegoarena.finder.agent import ClaudeFinder
from stegoarena.finder.search import StubSearch
from stegoarena.finder.trace import Trace
from stegoarena.generator.build import build_scheme
from stegoarena.generator.covers import make_cover
from stegoarena.genome import FinderGenome, child_finder, seed_population
from stegoarena.sandbox.local_provider import LocalProvider
from stegoarena.store import LocalStore

_ids = itertools.count()


def tool(name, inp):
    return NS(type="tool_use", id=f"tu{next(_ids)}", name=name, input=inp)


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.cache_dir, c.anthropic_api_key, c.finder_budget_s = tmp_path / "cache", "sk-test", 60
    c.cache_dir.mkdir()
    return c


def test_solvability_gate_and_biglist(tmp_path):
    with pytest.raises(SpecError):
        validate_spec(dict(technique="lsb", order="prng", key_source="secret"), allow_secret=False)
    with pytest.raises(SpecError):
        build_scheme(LocalStore(tmp_path), 1, "x", dict(technique="lsb", order="prng", key_source="secret"), seed=1)
    spec = validate_spec(dict(technique="dct", channels=["G"], dct_coeff="2,2", dct_delta=20, order="prng",
                              cipher="xor", key_source="biglist"))
    key = bigwordlist()[4321]
    img = load_png(save_png(embed(make_cover(2, 256), "FLAG{big_list_ok}", spec, key), spec, key))
    found = []
    st = search(img, bigwordlist(), techniques=["dct"], channels=["G"], deltas=[20], budget_s=30,
                on_hit=lambda f, s, k: found.append(k))
    assert key in found and st["carriers"] == 7


class RouterMessages:
    """Answers by conversation state, so parallel branches get the right script."""

    def __init__(self, flag):
        self.flag, self.calls = flag, []

    async def create(self, **kw):
        msgs = kw["messages"]
        last = json.dumps(msgs[-1]["content"], default=str)
        self.calls.append({"tools": kw["tools"], "n": len(msgs), "first": msgs[0], "second": msgs[1] if len(msgs) > 1 else None, "last": last})
        if len(msgs) == 1:
            return NS(stop_reason="tool_use", content=[tool("branch", {"hypotheses": [
                {"label": "lsb", "plan": "triage everything"}, {"label": "dct", "plan": "stall"}]})])
        if "EXTRACTED" in last:
            return NS(stop_reason="tool_use", content=[tool("submit_flag", {"flag": self.flag})])
        if "You are branch b0" in last:
            return NS(stop_reason="tool_use", content=[
                tool("save_tool", {"name": "quick_triage", "description": "runs triage", "code": "print('ok')"}),
                tool("run_shell", {"command": "python3 -m arena_schemes.triage challenge.png"})])
        if "You are branch b1" in last:
            return NS(stop_reason="tool_use", content=[tool("run_shell", {"command": "sleep 2; echo nothing"})])
        if 'branches' in last and 'extracted' in last:  # parent receives the branch summary
            br = json.loads(msgs[-1]["content"][0]["content"])["branches"]
            flag = next(f for b in br for f in b["extracted"])
            return NS(stop_reason="tool_use", content=[tool("submit_flag", {"flag": flag})])
        return NS(stop_reason="end_turn", content=[NS(type="text", text="done")])


def test_branch_forks_sandbox_and_conversation(tmp_path, cfg):
    store = LocalStore(tmp_path / "arena")
    scheme = build_scheme(store, 1, "s0", dict(technique="lsb", channels=["G"], cipher="xor", key_source="metadata"), seed=4)
    genome = FinderGenome(id="f-test", persona_name="t", persona="test persona", max_branches=2)

    async def go():
        prov = LocalProvider(cfg)
        sb = await prov.fork(await prov.create_from_template())
        await prov.write_bundle(sb, {"challenge.png": store.read_bytes(scheme.image_key)})
        await prov.exec(sb, "echo parent-state > state.txt")
        f = ClaudeFinder(cfg, StubSearch(), genome)
        fake = RouterMessages(scheme.flag)
        f.client = NS(beta=NS(messages=fake))
        return await f.run(prov, sb, Trace(store, 1, "a0")), fake

    res, fake = asyncio.run(go())
    assert res["submitted_flag"] == scheme.flag
    assert {b["label"].split(":")[0] for b in res["branches"]} == {"b0", "b1"}
    assert any(b["extracted"] == [scheme.flag] for b in res["branches"])
    assert "quick_triage" in res["pending_tools"]
    # both branches started from the parent's exact conversation prefix
    child_calls = [c for c in fake.calls if "You are branch" in c["last"]]
    assert len(child_calls) == 2
    parent_branch_msg = child_calls[0]["second"]
    assert all(c["n"] == 3 and c["first"] == fake.calls[0]["first"] and c["second"] is parent_branch_msg for c in child_calls)
    assert "branch" in [t["name"] for t in fake.calls[0]["tools"]]
    trace = (tmp_path / "arena/rounds/1/attempts/a0/trace.jsonl").read_text()
    assert '"lane": "b0:lsb"' in trace and '"type": "branch_result"' in trace


def test_static_finder_has_no_learning_tools(cfg):
    names = [t["name"] for t in ClaudeFinder(cfg, StubSearch()).tools]
    assert "branch" not in names and "save_tool" not in names


def test_learning_commits_only_valid_tools(tmp_path, cfg, monkeypatch):
    store = LocalStore(tmp_path / "arena")
    g = FinderGenome(id="f-l", persona_name="l", persona="p")
    g.save(store)
    attempts = [{"round": 1, "attempt_id": "a", "scheme_id": "s", "cracked": True, "score": 1.0,
                 "pending_tools": {"good_tool": {"description": "works", "code": "import sys; print(sys.argv[1])"},
                                   "bad_tool": {"description": "crashes", "code": "raise SystemExit(3)"}}}]

    async def fake_reflect(*a, **k):
        return learn.Reflection(lessons=["detect dct lattice first", "never trust trailer flags"],
                                keep_tools=["good_tool", "bad_tool"], retire_tools=[], insight="x")

    monkeypatch.setattr(learn, "reflect", fake_reflect)

    async def go():
        prov = LocalProvider(cfg)
        tmpl = await prov.create_from_template()
        return await learn.learn_from_round(cfg, store, prov, tmpl, g, attempts, {}, b"png", generation=1)

    out = asyncio.run(go())
    assert out["committed"] == ["good_tool"] and out["rejected"] == ["bad_tool"]
    g2 = FinderGenome.load(store, "f-l")
    assert g2.playbook == ["detect dct lattice first", "never trust trailer flags"]
    assert list(g2.tool_files(store)) == ["good_tool.py"]
    assert store.exists("genomes/finders/f-l/history/playbook.gen1.md")


def test_child_inherits_playbook_and_tools(tmp_path):
    store = LocalStore(tmp_path)
    pop = seed_population(store)
    assert len(pop["finders"]) == 3 and len(pop["hiders"]) == 3
    parent = FinderGenome.load(store, pop["finders"][0])
    parent.playbook = ["rule A"]
    parent.commit_tool(store, "lattice_scan", "dct detector", "print(1)")
    parent.save(store)
    child = child_finder(store, parent, "mutant", "new persona", generation=2)
    c2 = FinderGenome.load(store, child.id)
    assert c2.parent == parent.id and c2.playbook == ["rule A"] and "lattice_scan.py" in c2.tool_files(store)
