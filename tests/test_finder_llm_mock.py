"""ClaudeFinder loop against a fake Anthropic client: checks tool dispatch, tracing,
submit handling and the request shape - without spending tokens."""

import asyncio
from types import SimpleNamespace as NS

from stegoarena.config import Config
from stegoarena.finder.agent import ClaudeFinder
from stegoarena.finder.search import StubSearch
from stegoarena.finder.trace import Trace
from stegoarena.generator.build import build_scheme
from stegoarena.hider.ladder import KNOWN_EASY
from stegoarena.sandbox.local_provider import LocalProvider
from stegoarena.store import LocalStore


class FakeMessages:
    def __init__(self, script):
        self.script, self.calls = script, []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.script[len(self.calls) - 1]


def tool(id_, name, inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def test_finder_loop_with_fake_client(tmp_path):
    cfg = Config()
    cfg.cache_dir, cfg.anthropic_api_key = tmp_path / "cache", "sk-test"
    cfg.cache_dir.mkdir()
    store = LocalStore(tmp_path / "arena")
    scheme = build_scheme(store, 1, "s0", KNOWN_EASY, seed=3)

    script = [
        NS(stop_reason="tool_use", content=[NS(type="thinking", thinking="Start with triage."),
                                            tool("t1", "run_shell", {"command": "python3 -m arena_schemes.triage challenge.png --quick"}),
                                            tool("t2", "search_web", {"query": "lsb steganalysis"})]),
        NS(stop_reason="tool_use", content=[tool("t3", "report_signal", {"anomaly": "LSB payload", "technique": "lsb", "params": {"channels": ["R", "G", "B"]}}),
                                            tool("t4", "submit_flag", {"flag": scheme.flag})]),
    ]

    async def go():
        prov = LocalProvider(cfg)
        sb = await prov.fork(await prov.create_from_template())
        await prov.write_file(sb, "challenge.png", store.read_bytes(scheme.image_key))
        f = ClaudeFinder(cfg, StubSearch())
        f.client = NS(beta=NS(messages=FakeMessages(script)))
        res = await f.run(prov, sb, Trace(store, 1, "a0"))
        return res, f.client.beta.messages.calls

    res, calls = asyncio.run(go())
    assert res["submitted_flag"] == scheme.flag and res["stop_reason"] == "submitted"
    assert res["signals"][0]["technique"] == "lsb"
    # parallel tool results go back in ONE user message
    assert [b["tool_use_id"] for b in calls[1]["messages"][2]["content"]] == ["t1", "t2"]
    assert calls[0]["thinking"]["type"] == "adaptive" and calls[0]["fallbacks"] == "default"
    trace = (tmp_path / "arena/rounds/1/attempts/a0/trace.jsonl").read_text()
    assert '"type": "reasoning"' in trace and '"type": "search"' in trace
    assert "triage" in (tmp_path / "arena/rounds/1/attempts/a0/commands.log").read_text()
