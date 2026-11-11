"""Keenable use: unfamiliar-signal nudges, search->command linking, evidence-driven hider research."""

import asyncio
import itertools
import json
from types import SimpleNamespace as NS

from stegoarena.config import Config
from stegoarena.finder.agent import ClaudeFinder, unfamiliar_signals
from stegoarena.finder.search import SearchProvider
from stegoarena.finder.trace import Trace
from stegoarena.genome import FinderGenome
from stegoarena.hider.agent import ClaudeHider
from stegoarena.sandbox.local_provider import LocalProvider
from stegoarena.store import LocalStore

_ids = itertools.count()


def tool(name, inp):
    return NS(type="tool_use", id=f"k{next(_ids)}", name=name, input=inp)


class RecordingSearch(SearchProvider):
    name = "keenable-fake"

    def __init__(self):
        self.queries = []

    async def search(self, query, max_results=5):
        self.queries.append(query)
        return [{"title": f"About {query}", "url": "https://example.org/x", "snippet": "use zsteg -a"}]

    async def fetch(self, url, max_chars=6000):
        return {"url": url, "title": "page", "content": "..."}


def test_unfamiliar_signals_learned_away():
    out = '{"anomaly": "LSB pairs-of-values equalised in G (chi=0.20)"}\nchunk vpAg at offset 0x21'
    assert unfamiliar_signals(out, 0, []) == ["non-standard PNG chunk vpAg", "LSB pairs-of-values equalised in G (chi=0.20)"]
    book = ["pairs-of-values equalised in a channel => LSB in that channel; run triage --only lsb --channels",
            "vpAg is a harmless virtual-page chunk from ImageMagick; ignore it"]
    assert unfamiliar_signals(out, 0, book) == []
    assert unfamiliar_signals("usage: stegseek [--crack] stegofile wordlist", 2, []) == ["how to use stegseek"]
    assert unfamiliar_signals("usage: stegseek ...", 0, []) == []  # only on failed commands
    assert unfamiliar_signals('{"anomaly": "budget hit after 3/158 keys - narrow with --only"}', 0, []) == []


class Scripted:
    def __init__(self):
        self.calls = []

    async def create(self, **kw):
        last = json.dumps(kw["messages"][-1]["content"], default=str)
        self.calls.append(last)
        n = len(self.calls)
        if n == 1:  # tool output contains a statistical anomaly the playbook doesn't cover
            return NS(stop_reason="tool_use", content=[tool("run_shell", {"command": "echo '{\"anomaly\": \"LSB pairs-of-values equalised in G (chi=0.20)\"}'"})])
        if n == 2:
            assert "unfamiliar signal" in last  # harness nudge reached the model
            return NS(stop_reason="tool_use", content=[tool("search_web", {"query": "pairs of values chi-square LSB detection"})])
        if n == 3:
            return NS(stop_reason="tool_use", content=[tool("run_shell", {"command": "echo narrowed --only lsb --channels G"})])
        return NS(stop_reason="tool_use", content=[tool("submit_flag", {"flag": ""})])


def test_search_is_triggered_and_linked_to_next_command(tmp_path):
    cfg = Config()
    cfg.cache_dir, cfg.anthropic_api_key, cfg.finder_budget_s = tmp_path / "c", "sk-test", 60
    cfg.cache_dir.mkdir()
    store = LocalStore(tmp_path / "a")
    search = RecordingSearch()

    async def go():
        prov = LocalProvider(cfg)
        sb = await prov.fork(await prov.create_from_template())
        f = ClaudeFinder(cfg, search, FinderGenome(id="f-k", persona_name="k", persona="", max_branches=0))
        f.client = NS(beta=NS(messages=Scripted()))
        return await f.run(prov, sb, Trace(store, 1, "k0"))

    res = asyncio.run(go())
    ev = [json.loads(x) for x in (tmp_path / "a/rounds/1/attempts/k0/trace.jsonl").read_text().splitlines()]
    unf = next(e for e in ev if e["type"] == "unfamiliar")
    srch = next(e for e in ev if e["type"] == "search")
    cmds = [e for e in ev if e["type"] == "tool_call" and e["tool"] == "run_shell"]
    assert unf["signals"] == ["LSB pairs-of-values equalised in G (chi=0.20)"]
    assert srch["trigger"] == unf["signals"][0] and srch["provider"] == "keenable-fake"
    assert "informed_by" not in cmds[0]
    assert cmds[1]["informed_by"]["query"] == "pairs of values chi-square LSB detection"
    assert cmds[1]["informed_by"]["trigger"] == unf["signals"][0]
    assert res["searches"] == [{"query": "pairs of values chi-square LSB detection", "trigger": unf["signals"][0], "lane": "main"}]


def test_hider_research_queries_come_from_evidence():
    search = RecordingSearch()
    h = ClaudeHider(NS(anthropic_api_key="sk-test"), search)
    prev = [
        {"spec": {"technique": "dct"}, "cracked": True, "cracked_by": ["f1", "f2"],
         "signals": [{"anomaly": "coefficient lattice clustering at delta 18"}]},
        {"spec": {"technique": "lsb"}, "cracked": True, "cracked_by": ["f1"], "signals": []},
        {"spec": {"technique": "alpha"}, "cracked": False, "signals": []},
    ]
    notes = asyncio.run(h.research(prev, opponent_playbook=["try biglist with --only dct"]))
    qs = [n["query"] for n in notes]
    assert qs[0].startswith("DCT") and "lsb" in qs[1].lower()  # most-cracked technique first
    assert len(qs) == 3 and all(n["why"] and n["provider"] == "keenable-fake" for n in notes)
    assert asyncio.run(h.research([]))[0]["why"].startswith("no feedback")
