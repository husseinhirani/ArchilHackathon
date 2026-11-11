"""FastAPI UI. Reads the LOCAL store only, so the demo replay never needs the network."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from ..store import LocalStore

STATIC = Path(__file__).resolve().parent / "static"


def create_app(cfg) -> FastAPI:
    store = LocalStore(cfg.arena_root)
    app = FastAPI(title="StegoArena")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text()

    @app.get("/api/overview")
    def overview():
        rounds = store.read_json("rounds/index.json", {"rounds": []})["rounds"]
        return {
            "rounds": rounds,
            "summaries": {r: store.read_json(f"rounds/{r}/summary.json") for r in rounds},
            "research": {r: store.read_json(f"rounds/{r}/hider_research.json") for r in rounds if store.exists(f"rounds/{r}/hider_research.json")},
            "elo": store.read_json("elo.json", {"ratings": {}, "history": [], "meta": {}}),
            "lineage": store.read_json("lineage.json", {"nodes": {}}),
        }

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

    @app.get("/img/{round_no}/{scheme_id}.png")
    def image(round_no: int, scheme_id: str):
        p = store.path(f"rounds/{round_no}/schemes/{scheme_id}/image.png")
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/png")

    return app
