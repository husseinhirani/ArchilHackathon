"""Live arena HTTP API + page. Mounted by ui/app.py via `mount_live(app, arena)`."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from ..live import LiveArena, LiveError

STATIC = Path(__file__).resolve().parent / "static"
MAX_PHOTO = 12 * 1024 * 1024


class StartReq(BaseModel):
    photo_b64: str | None = None  # data URL or bare base64; omitted -> sample image
    hider: str | None = None  # hider genome id; omitted -> best by Elo
    mode: str = "fair"  # fair | allout


def mount_live(app: FastAPI, arena) -> LiveArena:
    live = LiveArena(arena)

    def guard(fn):
        try:
            return fn()
        except LiveError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/live", response_class=HTMLResponse)
    def page():
        return (STATIC / "live.html").read_text()

    @app.get("/api/live/options")
    def options():
        return live.options()

    @app.get("/api/live/recent")
    def recent():
        return live.recent()

    @app.post("/api/live")
    async def start(req: StartReq):
        photo = None
        if req.photo_b64:
            try:
                photo = base64.b64decode(req.photo_b64.split(",", 1)[-1], validate=False)
            except Exception as e:
                raise HTTPException(400, "That upload didn't decode as an image.") from e
            if len(photo) > MAX_PHOTO:
                raise HTTPException(413, "Photo too large (12 MB max).")
        try:
            return {"lid": await live.start(photo, req.hider, req.mode)}
        except LiveError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/live/{lid}/state")
    def state(lid: str, since: str = "{}"):
        try:
            offsets = {k: int(v) for k, v in json.loads(since).items()}
        except Exception:
            offsets = {}
        return guard(lambda: live.state(lid, offsets))

    @app.get("/api/live/{lid}/{which}.png")
    def image(lid: str, which: str):
        meta = live.store.read_json(f"live/{lid}/meta.json")
        if not meta or which not in ("original", "stego"):
            raise HTTPException(404)
        key = f"live/{lid}/original.png" if which == "original" else f"rounds/{meta['round']}/schemes/live/image.png"
        if not live.store.exists(key):
            raise HTTPException(404)
        return Response(live.store.read_bytes(key), media_type="image/png", headers={"Cache-Control": "no-store"})

    return live
