"""Hide & Seek HTTP API + pages. Mounted by ui/app.py via `mount_hideseek(app, cfg)`.

Truth never leaves the server until a race is over (see HideSeek.public)."""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from ..hideseek import LEVEL_BLURB, LEVELS, ChallengeError, HideSeek, base, build_challenge, inspect_png, try_decode

STATIC = Path(__file__).resolve().parent / "static"
MAX_PHOTO = 12 * 1024 * 1024
HS_RE = re.compile(r"HS1:[^\"\n\\]{1,120}")


def redact(obj):
    """Mask recovered messages in AI events while the race is on (otherwise the human copies them)."""
    if isinstance(obj, str):
        return HS_RE.sub("HS1:\u2588\u2588\u2588\u2588 (hidden until the race ends)", obj)
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    return obj


class CreateReq(BaseModel):
    message: str
    level: str = "easy"
    hint: str = ""
    photo_b64: str | None = None  # data URL or bare base64; omitted -> procedural cover


class TryReq(BaseModel):
    spec: dict
    key: str | None = None
    wordlist: bool = False


class GuessReq(BaseModel):
    message: str


def mount_hideseek(app: FastAPI, cfg, *, arena=None) -> HideSeek:
    if arena is None:
        from ..orchestrator import Arena

        arena = Arena(cfg, finder="llm", hider="ladder")  # falls back to scripted without an API key
    hs = HideSeek(arena)
    store = hs.store
    last_try: dict[str, float] = {}

    def guard(fn):
        try:
            return fn()
        except ChallengeError as e:
            raise HTTPException(400, str(e)) from e

    def page():
        return HTMLResponse((STATIC / "hs.html").read_text())

    app.get("/hs", response_class=HTMLResponse)(page)
    app.get("/c/{cid}", response_class=HTMLResponse)(lambda cid: page())

    @app.get("/api/hs/levels")
    def levels():
        return {"levels": {k: {"blurb": LEVEL_BLURB[k]} for k in LEVELS}, "finder": arena.finder_kind,
                "provider": arena.provider.name, "search": arena.search.name}

    @app.post("/api/c")
    def create(req: CreateReq):
        photo = None
        if req.photo_b64:
            raw = req.photo_b64.split(",", 1)[-1]
            try:
                photo = base64.b64decode(raw, validate=False)
            except Exception as e:
                raise HTTPException(400, f"bad photo encoding: {e}") from e
            if len(photo) > MAX_PHOTO:
                raise HTTPException(413, "photo too large (12 MB max)")
        cid = guard(lambda: build_challenge(store, photo, req.message, req.level, req.hint))
        return {"cid": cid, "play_url": f"/c/{cid}", "watch_url": f"/c/{cid}?watch=1"}

    @app.get("/api/c/{cid}")
    def get(cid: str):
        return guard(lambda: hs.public(cid))

    @app.get("/hs/img/{cid}.png")
    def image(cid: str):
        key = guard(lambda: f"{base(cid)}/image.png")
        if not store.exists(key):
            raise HTTPException(404)
        return Response(store.read_bytes(key), media_type="image/png")

    @app.post("/api/c/{cid}/start")
    async def start(cid: str, watch: bool = False):
        try:
            race = await hs.start(cid, human=not watch)
        except ChallengeError as e:
            raise HTTPException(400, str(e)) from e
        return {"race": race}

    @app.get("/api/c/{cid}/state")
    def state(cid: str, ai_i: int = 0, h_i: int = 0):
        pub = guard(lambda: hs.public(cid))
        ai = hs.events(cid, "ai", ai_i)
        if pub["race"].get("status") != "done":
            ai = [redact({**e, **({"flag": "\u2588\u2588\u2588\u2588"} if e.get("type") == "submit" else {})}) for e in ai]
        return {**pub, "ai": ai, "human": hs.events(cid, "human", h_i), "now": time.time()}

    @app.get("/api/c/{cid}/inspect")
    def inspect(cid: str):
        key = guard(lambda: f"{base(cid)}/image.png")
        if not store.exists(key):
            raise HTTPException(404)
        out = inspect_png(store.read_bytes(key))
        if hs.race(cid).get("status") == "racing":
            hs.human_event(cid, "inspect", text_keys=list(out["text"]), trailer_bytes=out["trailer_bytes"])
        return out

    @app.post("/api/c/{cid}/try")
    def try_(cid: str, req: TryReq):
        key = guard(lambda: f"{base(cid)}/image.png")
        if not store.exists(key):
            raise HTTPException(404)
        now = time.monotonic()
        if now - last_try.get(cid, 0.0) < 0.7:
            raise HTTPException(429, "slow down: one try per second")
        last_try[cid] = now
        res = try_decode(store.read_bytes(key), req.spec, req.key, req.wordlist)
        if hs.race(cid).get("status") == "racing":
            hs.human_event(cid, "try", spec={k: req.spec.get(k) for k in ("technique", "channels", "bit_plane", "order", "cipher")},
                           key=req.key, wordlist=req.wordlist, found=res.get("found"))
        return res  # a found message is not a win until the seeker submits it as a guess

    @app.post("/api/c/{cid}/guess")
    async def guess(cid: str, req: GuessReq):
        try:
            if hs.race(cid).get("status") == "waiting":
                raise ChallengeError("press start first")
            return await hs.guess(cid, req.message)
        except ChallengeError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/c/{cid}/giveup")
    async def giveup(cid: str):
        return guard(lambda: None) or await hs.give_up(cid)

    @app.get("/api/hs/stats")
    def stats():
        return hs.stats()

    return hs
