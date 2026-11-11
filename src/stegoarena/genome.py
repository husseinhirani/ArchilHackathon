"""Agent genomes: everything that makes one agent different from another EXCEPT the model.

A finder genome = persona + playbook (learned lessons) + toolkit (scripts it wrote) + branch policy.
A hider genome  = strategy + memory of what it fielded and how that went.
All of it lives on the shared arena store (mirrored to the Archil disk), so an agent's
"mind" persists across rounds and is loaded into every sandbox fork it runs in.

Layout:
    genomes/finders/{gid}/genome.json | playbook.md | tools/index.json | tools/{name}.py
    genomes/hiders/{gid}/genome.json  | memory.md
    population.json
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field

from .store import ArenaStore

FINDER_PERSONAS = {
    "statistician": "You are the STATISTICIAN. Detect before you search: measure (histograms, lattice "
                    "clustering of DCT coefficients, bit-plane statistics, alpha/palette structure) to pin the "
                    "technique and parameters, THEN run a narrowed key search (--only/--channels/--deltas/--bit-planes).",
    "locksmith": "You are the LOCKSMITH. Keys are your obsession: harvest every candidate key (metadata, strings, "
                 "wordlist, biglist), and spend your time on the widest key search the clock allows, narrowing just "
                 "enough to make big keyspaces fit the budget.",
    "scout": "You are the SCOUT. Map the terrain quickly with cheap probes across every tool, read the scheme "
             "source when unsure, and research unfamiliar signals before committing to one hypothesis.",
}
HIDER_STRATEGIES = {
    "decoy_artist": "You are the DECOY ARTIST: misdirection first. Use decoys and keys hidden in plain sight so a "
                    "hasty finder grabs the wrong flag, while the real payload sits somewhere less obvious.",
    "frequency_purist": "You are the FREQUENCY PURIST: favour DCT-domain embedding with unusual coefficients and "
                        "deltas, keyed ordering, and biglist keys, so naive spatial tools see nothing.",
    "key_smith": "You are the KEY SMITH: make the search space the weapon. Keyed prng ordering, ciphers and "
                 "biglist keys on carriers that are hard to narrow (multi-channel LSB, higher bit planes).",
}


def _gid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:5]}"


@dataclass
class FinderGenome:
    id: str
    persona_name: str
    persona: str
    parent: str | None = None
    generation: int = 0
    max_branches: int = 3
    created: float = field(default_factory=time.time)
    playbook: list[str] = field(default_factory=list)
    tools: dict[str, dict] = field(default_factory=dict)  # name -> {description, uses, wins, gen}
    learning: bool = True  # False = static baseline (no playbook / tools / reflection)

    @property
    def base(self) -> str:
        return f"genomes/finders/{self.id}"

    def save(self, store: ArenaStore, tag: str | None = None) -> None:
        meta = {k: v for k, v in asdict(self).items() if k not in ("playbook", "tools")}
        store.write_json(f"{self.base}/genome.json", meta)
        store.write_bytes(f"{self.base}/playbook.md", ("\n".join(f"- {b}" for b in self.playbook) + "\n").encode())
        store.write_json(f"{self.base}/tools/index.json", self.tools)
        if tag:  # snapshot so the UI can show how the mind changed over generations
            store.write_bytes(f"{self.base}/history/playbook.{tag}.md",
                              ("\n".join(f"- {b}" for b in self.playbook) + "\n").encode())

    @classmethod
    def load(cls, store: ArenaStore, gid: str) -> "FinderGenome":
        base = f"genomes/finders/{gid}"
        g = cls(**store.read_json(f"{base}/genome.json"))
        if store.exists(f"{base}/playbook.md"):
            g.playbook = [ln[2:].strip() for ln in store.read_bytes(f"{base}/playbook.md").decode().splitlines() if ln.startswith("- ")]
        g.tools = store.read_json(f"{base}/tools/index.json", {})
        return g

    def tool_files(self, store: ArenaStore) -> dict[str, bytes]:
        return {f"{name}.py": store.read_bytes(f"{self.base}/tools/{name}.py") for name in self.tools
                if store.exists(f"{self.base}/tools/{name}.py")}

    def commit_tool(self, store: ArenaStore, name: str, description: str, code: str) -> None:
        store.write_bytes(f"{self.base}/tools/{name}.py", code.encode())
        prev = self.tools.get(name, {})
        self.tools[name] = {"description": description, "uses": prev.get("uses", 0), "wins": prev.get("wins", 0),
                            "gen": prev.get("gen", self.generation)}


@dataclass
class HiderGenome:
    id: str
    strategy_name: str
    strategy: str
    parent: str | None = None
    generation: int = 0
    created: float = field(default_factory=time.time)
    memory: list[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"genomes/hiders/{self.id}"

    def save(self, store: ArenaStore) -> None:
        store.write_json(f"{self.base}/genome.json", {k: v for k, v in asdict(self).items() if k != "memory"})
        store.write_bytes(f"{self.base}/memory.md", ("\n".join(f"- {m}" for m in self.memory[-40:]) + "\n").encode())

    @classmethod
    def load(cls, store: ArenaStore, gid: str) -> "HiderGenome":
        base = f"genomes/hiders/{gid}"
        g = cls(**store.read_json(f"{base}/genome.json"))
        if store.exists(f"{base}/memory.md"):
            g.memory = [ln[2:].strip() for ln in store.read_bytes(f"{base}/memory.md").decode().splitlines() if ln.startswith("- ")]
        return g


TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


def seed_population(store: ArenaStore) -> dict:
    finders = []
    for name, persona in FINDER_PERSONAS.items():
        g = FinderGenome(id=_gid(f"f-{name}"), persona_name=name, persona=persona)
        g.save(store)
        finders.append(g.id)
    hiders = []
    for name, strat in HIDER_STRATEGIES.items():
        h = HiderGenome(id=_gid(f"h-{name}"), strategy_name=name, strategy=strat)
        h.save(store)
        hiders.append(h.id)
    pop = {"generation": 0, "finders": finders, "hiders": hiders, "retired": [], "history": []}
    store.write_json("population.json", pop)
    return pop


def static_finder() -> FinderGenome:
    """Baseline: same model, base prompt only, no playbook, no tools, no branching."""
    return FinderGenome(id="static", persona_name="static", persona="", max_branches=0, learning=False)


def child_finder(store: ArenaStore, parent: FinderGenome, persona_name: str, persona: str, generation: int) -> FinderGenome:
    """Inherit the parent's playbook and toolkit (files copied), with a new persona."""
    child = FinderGenome(id=_gid(f"f-{persona_name}"), persona_name=persona_name, persona=persona, parent=parent.id,
                         generation=generation, max_branches=parent.max_branches,
                         playbook=list(parent.playbook), tools={k: dict(v) for k, v in parent.tools.items()})
    for fname, data in parent.tool_files(store).items():
        store.write_bytes(f"{child.base}/tools/{fname}", data)
    child.save(store)
    return child


def child_hider(parent: HiderGenome, strategy_name: str, strategy: str, generation: int) -> HiderGenome:
    return HiderGenome(id=_gid(f"h-{strategy_name}"), strategy_name=strategy_name, strategy=strategy,
                       parent=parent.id, generation=generation, memory=list(parent.memory))
