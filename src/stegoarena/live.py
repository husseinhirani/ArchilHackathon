"""Live arena: a visitor uploads a photo and watches the HIDER and FINDER agents run in real time.

Pipeline (one asyncio task per match, every step traced to the shared store):
  1. hider genome (best by Elo, or the visitor's pick) researches on Keenable and designs a scheme
     for THIS photo (reasoning summaries are streamed as events);
  2. the scheme is embedded into the uploaded photo, round-trip verified, with proof the change is
     invisible (pixels changed, max change);
  3. 3-4 finders race in parallel Archil forks: the top evolved genomes, the bare model and the
     scripted baseline, each in its own lane;
  4. results + reveal. Live matches never touch Elo or the round index (the Lab stays clean).

Layout: live/index.json, live/{lid}/meta.json, live/{lid}/original.png, and the usual round files
under rounds/{7000+n}/ (schemes/live/*, attempts/{lane}/trace.jsonl, attempts/hider/trace.jsonl).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import secrets
import time

import numpy as np
from PIL import Image

from .arena_schemes import difficulty, load_png, validate_spec
from .finder.trace import Trace
from .generator.build import GenerationError, build_scheme
from .generator.covers import make_cover
from .genome import FinderGenome, HiderGenome, static_finder
from .hider.ladder import LADDER, mutate
from .scorer import FINDER_START, EloBook

log = logging.getLogger(__name__)
LIVE_ROUND_BASE = 7000
MAX_LIVE = 2  # concurrent matches
MIN_SIDE = 96
MODES = {
    "fair": "FAIR FIGHT: this is a live demo race. Design something clever and subtle, but a strong finder must be able "
            "to crack it within 90 seconds: key_source must be none, metadata or wordlist (no biglist).",
    "allout": "ALL OUT: make it as hard to find as you can within the rules (biglist keys allowed).",
}


def enforce_mode(spec, mode: str):
    """Fair fight: biglist keys can't be searched in 90s without perfect narrowing, so cap at the wordlist."""
    if mode == "fair" and spec.key_source == "biglist":
        return validate_spec({**spec.to_dict(), "key_source": "wordlist"}), True
    return spec, False


class LiveError(ValueError):
    pass


def prepare_photo(photo: bytes | None) -> tuple[Image.Image, str]:
    if not photo:
        return make_cover(random.randrange(1 << 30), 512, grain=0.0), "sample"
    try:
        img = Image.open(io.BytesIO(photo))
        img.load()
    except Exception as e:
        raise LiveError(f"Couldn't read that image ({e.__class__.__name__}). Try a JPG or PNG.") from e
    img = img.convert("RGB")
    img.thumbnail((512, 512))
    if min(img.size) < MIN_SIDE:
        raise LiveError(f"That image is too small ({img.width}x{img.height}). Use one at least {MIN_SIDE}px on each side.")
    return img, "upload"


def invisibility(original: Image.Image, stego_png: bytes) -> dict:
    a = np.asarray(original.convert("RGB"), dtype=np.int16)
    b = np.asarray(load_png(stego_png).convert("RGB"), dtype=np.int16)
    if a.shape != b.shape:  # palette/DCT prep can't change size, but be safe
        return {}
    d = np.abs(a - b)
    changed = (d > 0).any(axis=2)
    return {"pixels_changed_pct": round(100 * float(changed.mean()), 3), "max_change": int(d.max()),
            "mean_change": round(float(d.mean()), 4)}


class LiveArena:
    def __init__(self, arena):
        self.arena = arena
        self.store = arena.store
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lineup
    def _population(self):
        pop = self.store.read_json("population.json")
        if not pop:
            return [], []
        elo = EloBook(self.store).data["ratings"]
        finders = sorted((FinderGenome.load(self.store, g) for g in pop["finders"]),
                         key=lambda f: -elo.get(f"finder:{f.id}", FINDER_START))
        hiders = sorted((HiderGenome.load(self.store, h) for h in pop["hiders"]),
                        key=lambda h: -elo.get(f"hider:{h.id}", 1000))
        return finders, hiders

    def options(self) -> dict:
        finders, hiders = self._population()
        cfg = self.arena.cfg
        return {"hiders": [{"id": h.id, "name": h.strategy_name.replace("_", " "), "strategy": h.strategy} for h in hiders],
                "lineup": [l["label"] for l in self._lineup(finders)],
                "llm": bool(cfg.anthropic_api_key), "provider": self.arena.provider.name, "search": self.arena.search.name,
                "finder_model": cfg.finder_model, "hider_model": cfg.hider_model, "budget_s": cfg.finder_budget_s}

    def _lineup(self, finders: list[FinderGenome]) -> list[dict]:
        lanes = []
        if self.arena.cfg.anthropic_api_key:
            for f in finders[:2]:
                lanes.append({"kind": "genome", "finder": f, "label": f.persona_name.replace("_", " "),
                              "sub": f"evolved · gen {f.generation} · {len(f.playbook)} rules · {len(f.tools)} tools"})
            lanes.append({"kind": "static", "finder": static_finder(), "label": "bare model", "sub": "no persona, playbook or tools"})
        lanes.append({"kind": "scripted", "finder": "scripted", "label": "scripted baseline", "sub": "fixed triage script, no AI"})
        return lanes

    # -------------------------------------------------------------- start
    async def start(self, photo: bytes | None, hider_id: str | None = None, mode: str = "fair") -> str:
        if mode not in MODES:
            raise LiveError("Mode must be 'fair' or 'allout'.")
        running = [t for t in self._tasks.values() if not t.done()]
        if len(running) >= MAX_LIVE:
            raise LiveError("Two live matches are already running. Give them a minute.")
        img, source = prepare_photo(photo)
        async with self._lock:
            idx = self.store.read_json("live/index.json", {"matches": []})
            lid = secrets.token_hex(4)
            round_no = LIVE_ROUND_BASE + len(idx["matches"]) + 1
            idx["matches"].append({"lid": lid, "round": round_no, "created": time.time()})
            self.store.write_json("live/index.json", idx)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self.store.write_bytes(f"live/{lid}/original.png", buf.getvalue())
        self._meta(lid, lid=lid, round=round_no, status="hiding", stage=1, created=time.time(), source=source,
                   width=img.width, height=img.height, lanes=[], hider=None, mode=mode)
        self._tasks[lid] = asyncio.create_task(self._run(lid, img, hider_id, mode))
        return lid

    def _meta(self, match_id: str, **fields) -> dict:
        m = self.store.read_json(f"live/{match_id}/meta.json", {})
        m.update(fields)
        self.store.write_json(f"live/{match_id}/meta.json", m)
        return m

    # ---------------------------------------------------------------- run
    async def _run(self, lid: str, img: Image.Image, hider_id: str | None, mode: str = "fair") -> None:
        meta = self.store.read_json(f"live/{lid}/meta.json")
        R, cfg, store = meta["round"], self.arena.cfg, self.store
        ht = Trace(store, R, "hider")
        try:
            finders, hiders = self._population()
            hider = next((h for h in hiders if h.id == hider_id), hiders[0] if hiders else None)
            hname = hider.strategy_name.replace("_", " ") if hider else "ladder"
            self._meta(lid, hider={"id": getattr(hider, "id", None), "name": hname, "strategy": getattr(hider, "strategy", "")})
            ht.log("start", hider=getattr(hider, "id", "ladder"), name=hname, model=cfg.hider_model if cfg.anthropic_api_key else "ladder (no API key)")

            # 1. research + design
            spec, why = None, ""
            if cfg.anthropic_api_key:
                from .hider.agent import ClaudeHider

                ch = ClaudeHider(cfg, self.arena.search)
                pop = store.read_json("population.json") or {}
                prev = store.read_json(f"rounds/{pop.get('generation', 0)}/summary.json", {}).get("schemes", []) if pop else []
                playbook = finders[0].playbook if finders else []
                ht.log("thinking", text="Researching how the finders crack schemes…")
                notes = await ch.research(prev, opponent_playbook=playbook)
                for n in notes:
                    ht.log("search", provider=n["provider"], query=n["query"], why=n.get("why"), titles=[h["title"] for h in n["results"]])
                if playbook:
                    ht.log("opponent", text=f"Read the top finder's playbook ({len(playbook)} rules) on the shared Archil disk.",
                           sample=playbook[:3])
                ht.log("thinking", text="Designing a scheme for this photo…")
                props = await ch.propose(R, 1, [], notes, strategy=(getattr(hider, "strategy", "") + "\n\n" + MODES[mode]).strip(),
                                         memory=getattr(hider, "memory", []),
                                         opponent_playbook=playbook, effort="medium",
                                         on_event=lambda t, **d: ht.log(t, **d))
                spec, _, why = props[0]
            else:
                spec, why = validate_spec(dict(random.choice(LADDER[3:]))), "ladder rung (no API key: offline hider)"
            spec, capped = enforce_mode(spec, mode)
            if capped:
                ht.log("thinking", text="Fair-fight rule: biglist key swapped for a wordlist key so it's crackable in 90s.")
            ht.log("design", spec=spec.to_dict(), rationale=why, difficulty=difficulty(spec), mode=mode)
            self._meta(lid, stage=2, status="embedding")

            # 2. embed into the visitor's photo (fallback to a mutated ladder rung if the photo can't carry it)
            built = None
            for attempt, sp in enumerate([spec, mutate(dict(LADDER[4]), random.Random(R)), validate_spec(dict(LADDER[0]))]):
                try:
                    built = build_scheme(store, R, "live", sp, seed=R * 7 + attempt, rationale=why, cover=img,
                                         cover_name=meta["source"], retries=2)
                    break
                except GenerationError as e:
                    ht.log("error", message=f"Couldn't embed that design in this photo ({e}); trying a simpler one.")
            if built is None:
                raise LiveError("This photo couldn't carry any scheme. Try a larger photo.")
            proof = invisibility(img, store.read_bytes(built.image_key))
            ht.log("embedded", difficulty=built.difficulty, spec=built.spec.to_dict(), **proof)
            ht.result({"spec": built.spec.to_dict(), "difficulty": built.difficulty, "rationale": why, **proof})

            # 3. finders race
            lanes = self._lineup(finders)
            for l in lanes:
                l["aid"] = f"live-{l['kind']}-{secrets.token_hex(2)}"
            self._meta(lid, stage=3, status="hunting", spec=built.spec.to_dict(), difficulty=built.difficulty,
                       rationale=why, proof=proof, hunt_started=time.time(),
                       lanes=[{k: v for k, v in l.items() if k != "finder"} for l in lanes])
            results = await asyncio.gather(*(self.arena.attempt(R, built, 0, l["finder"], attempt_id=l["aid"]) for l in lanes),
                                           return_exceptions=True)

            # 4. results + reveal
            out = []
            for l, r in zip(lanes, results):
                if isinstance(r, Exception):
                    r = {"cracked": False, "score": 0.0, "stop_reason": f"crash: {r}"}
                out.append({"aid": l["aid"], "label": l["label"], "cracked": bool(r.get("cracked")), "score": r.get("score"),
                            "elapsed_s": r.get("elapsed_s"), "decoy_submitted": bool(r.get("decoy_submitted")),
                            "stop": r.get("stop_reason"), "fork_s": r.get("fork_s")})
            winners = sorted((o for o in out if o["cracked"]), key=lambda o: o["elapsed_s"] or 1e9)
            truth = store.read_json(f"rounds/{R}/schemes/live/truth.json")
            self._meta(lid, stage=4, status="done", results=out, winner=winners[0]["label"] if winners else None,
                       reveal={"flag": truth["flag"], "key": truth.get("key"), "decoy": truth.get("decoy_flag")}, finished=time.time())
        except Exception as e:
            log.exception("live match %s failed", lid)
            ht.log("error", message=str(e))
            self._meta(lid, status="error", error=str(e) if isinstance(e, LiveError) else "Something went wrong; see the server log.")

    # -------------------------------------------------------------- state
    def _events(self, R: int, aid: str, since: int) -> list[dict]:
        key = f"rounds/{R}/attempts/{aid}/trace.jsonl"
        if not self.store.exists(key):
            return []
        lines = self.store.read_bytes(key).decode().splitlines()
        return [json.loads(x) for x in lines[since:] if x.strip()]

    def state(self, lid: str, since: dict[str, int]) -> dict:
        meta = self.store.read_json(f"live/{lid}/meta.json")
        if not meta:
            raise LiveError("No such match.")
        R = meta["round"]
        out = {"meta": meta, "now": time.time(), "hider": self._events(R, "hider", since.get("hider", 0)), "lanes": {}}
        for l in meta.get("lanes", []):
            out["lanes"][l["aid"]] = self._events(R, l["aid"], since.get(l["aid"], 0))
        return out

    def recent(self, n: int = 8) -> list[dict]:
        out = []
        for m in reversed(self.store.read_json("live/index.json", {"matches": []})["matches"][-n:]):
            meta = self.store.read_json(f"live/{m['lid']}/meta.json") or {}
            out.append({"lid": m["lid"], "status": meta.get("status"), "winner": meta.get("winner"),
                        "hider": (meta.get("hider") or {}).get("name"), "created": meta.get("created"),
                        "technique": (meta.get("spec") or {}).get("technique")})
        return out
