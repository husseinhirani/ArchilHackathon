"""FastAPI UI. Reads the LOCAL store only, so the demo replay never needs the network."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..store import LocalStore
from .highlights import attempt_meta, compute_highlights

STATIC = Path(__file__).resolve().parent / "static"


def create_app(cfg) -> FastAPI:
    store = LocalStore(cfg.arena_root)
    app = FastAPI(title="StegoArena")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")  # shared stylesheet for every page

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text()

    @app.get("/guide", response_class=HTMLResponse)
    def guide():
        return (STATIC / "guide.html").read_text()

    @app.get("/api/overview")
    def overview():
        rounds = store.read_json("rounds/index.json", {"rounds": []})["rounds"]
        summaries = {r: store.read_json(f"rounds/{r}/summary.json") for r in rounds}
        return {
            "rounds": rounds,
            "summaries": summaries,
            "attempt_meta": {r: attempt_meta(store, r, [a for n in (s or {}).get("schemes", []) for a in n.get("attempts", [])])
                             for r, s in summaries.items()},
            "research": {r: store.read_json(f"rounds/{r}/hider_research.json") for r in rounds if store.exists(f"rounds/{r}/hider_research.json")},
            "elo": store.read_json("elo.json", {"ratings": {}, "history": [], "meta": {}}),
            "lineage": store.read_json("lineage.json", {"nodes": {}}),
        }

    cache: dict = {}

    @app.get("/api/highlights")
    def highlights():
        sig = len(store.list("rounds")) + len(store.list("genomes"))  # cheap change detector
        if cache.get("sig") != sig:
            cache.update(sig=sig, data=compute_highlights(store))
        return cache["data"]

    @app.get("/api/attempt/{round_no}/{attempt_id}")
    def attempt(round_no: int, attempt_id: str):
        base = f"rounds/{round_no}/attempts/{attempt_id}"
        if not store.exists(f"{base}/trace.jsonl"):
            raise HTTPException(404)
        trace = [json.loads(line) for line in store.read_bytes(f"{base}/trace.jsonl").decode().splitlines() if line.strip()]
        return {"result": store.read_json(f"{base}/result.json"), "trace": trace}

    @app.get("/api/scheme/{round_no}/{scheme_id}")
    def scheme(round_no: int, scheme_id: str):
        base = f"rounds/{round_no}/schemes/{scheme_id}"
        return {"spec": store.read_json(f"{base}/spec.json"), "truth": store.read_json(f"{base}/truth.json")}

    @app.get("/api/agents")
    def agents():
        pop = store.read_json("population.json")
        elo = store.read_json("elo.json", {"ratings": {}})["ratings"]
        out = {"population": pop, "finders": [], "hiders": [], "eval": []}
        if pop:
            for gid in pop["finders"] + [r for r in pop.get("retired", []) if r.startswith("f-")]:
                base = f"genomes/finders/{gid}"
                if not store.exists(f"{base}/genome.json"):
                    continue
                g = store.read_json(f"{base}/genome.json")
                pb = store.read_bytes(f"{base}/playbook.md").decode() if store.exists(f"{base}/playbook.md") else ""
                learned = [json.loads(l) for l in store.read_bytes(f"{base}/learning.jsonl").decode().splitlines()] if store.exists(f"{base}/learning.jsonl") else []
                out["finders"].append({**g, "active": gid in pop["finders"], "elo": elo.get(f"finder:{gid}"),
                                       "playbook": [l[2:] for l in pb.splitlines() if l.startswith("- ")],
                                       "tools": store.read_json(f"{base}/tools/index.json", {}), "learning": learned})
            for gid in pop["hiders"] + [r for r in pop.get("retired", []) if r.startswith("h-")]:
                base = f"genomes/hiders/{gid}"
                if store.exists(f"{base}/genome.json"):
                    h = store.read_json(f"{base}/genome.json")
                    mem = store.read_bytes(f"{base}/memory.md").decode() if store.exists(f"{base}/memory.md") else ""
                    out["hiders"].append({**h, "active": gid in pop["hiders"], "elo": elo.get(f"hider:{gid}"),
                                          "memory": [l[2:] for l in mem.splitlines() if l.startswith("- ")]})
        for label in store.read_json("eval/index.json", {"runs": []})["runs"]:
            out["eval"].append(store.read_json(f"eval/results/{label}.json"))
        return out

    @app.get("/api/tool/{gid}/{name}")
    def tool_code(gid: str, name: str):
        key = f"genomes/finders/{gid}/tools/{name}.py"
        if not store.exists(key):
            raise HTTPException(404)
        return {"code": store.read_bytes(key).decode()}

    @app.get("/img/{round_no}/{scheme_id}.png")
    def image(round_no: int, scheme_id: str):
        p = store.path(f"rounds/{round_no}/schemes/{scheme_id}/image.png")
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/png")

    from .hideseek_routes import mount_hideseek  # Hide & Seek: /hs, /c/{cid}, /api/c/*

    hs = mount_hideseek(app, cfg)

    from .live_routes import mount_live  # Live arena: /live, /api/live/* (shares the Hide & Seek arena + sandbox template)

    mount_live(app, hs.arena)

    from .learn_routes import mount_learn  # Learn mode: /learn, /api/learn/*

    mount_learn(app, cfg, store=store)
    return app
