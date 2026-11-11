"""Learn mode HTTP API + page. Mounted by ui/app.py via `mount_learn(app, cfg, store=...)`."""

from __future__ import annotations

import base64
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .. import academy
from ..hideseek import ChallengeError

STATIC = Path(__file__).resolve().parent / "static"
MAX_PHOTO = 12 * 1024 * 1024
IMAGES = {"image", "cover", "diff"}


class HideReq(BaseModel):
    message: str
    photo_b64: str | None = None


class TryReq(BaseModel):
    spec: dict
    key: str | None = None
    wordlist: bool = False


class AnswerReq(BaseModel):
    message: str


class TutorReq(BaseModel):
    message: str
    step: str = "practice"


def mount_learn(app: FastAPI, cfg, *, store) -> None:
    tutor = academy.Tutor(cfg)
    last: dict[str, float] = {}

    def guard(fn):
        try:
            return fn()
        except ChallengeError as e:
            raise HTTPException(400, str(e)) from e

    def throttle(sid: str, gap: float) -> None:
        now = time.monotonic()
        if now - last.get(sid, 0.0) < gap:
            raise HTTPException(429, "slow down")
        last[sid] = now

    def page():
        return HTMLResponse((STATIC / "learn.html").read_text())

    app.get("/learn", response_class=HTMLResponse)(page)
    app.get("/learn/{level}", response_class=HTMLResponse)(lambda level: page())

    @app.get("/api/learn/lessons")
    def lessons():
        return {"steps": academy.STEPS, "lessons": academy.LESSONS, "tutor": "claude" if cfg.anthropic_api_key else "canned"}

    @app.post("/api/learn/{level}/hide")
    def hide(level: str, req: HideReq):
        photo = None
        if req.photo_b64:
            try:
                photo = base64.b64decode(req.photo_b64.split(",", 1)[-1])
            except Exception as e:
                raise HTTPException(400, f"bad photo encoding: {e}") from e
            if len(photo) > MAX_PHOTO:
                raise HTTPException(413, "photo too large (12 MB max)")
        sid = guard(lambda: academy.start_hide(store, level, req.message, photo))
        return guard(lambda: academy.public(store, sid))

    @app.post("/api/learn/{level}/practice")
    def practice(level: str):
        sid = guard(lambda: academy.start_practice(store, level))
        return guard(lambda: academy.public(store, sid))

    @app.get("/api/learn/s/{sid}")
    def get(sid: str):
        return guard(lambda: academy.public(store, sid))

    @app.get("/api/learn/s/{sid}/img/{name}.png")
    def img(sid: str, name: str):
        if name not in IMAGES:
            raise HTTPException(404)
        key = guard(lambda: f"{academy.base(sid)}/{name}.png")
        if not store.exists(key):
            raise HTTPException(404)
        return Response(store.read_bytes(key), media_type="image/png")

    @app.get("/api/learn/s/{sid}/signals")
    def sigs(sid: str):
        return guard(lambda: academy.session_signals(store, sid))

    @app.post("/api/learn/s/{sid}/try")
    def try_(sid: str, req: TryReq):
        throttle(sid, 0.7)
        return guard(lambda: academy.try_(store, sid, req.spec, req.key, req.wordlist))

    @app.post("/api/learn/s/{sid}/check")
    def check(sid: str, req: AnswerReq):
        return guard(lambda: academy.check(store, sid, req.message))

    @app.post("/api/learn/s/{sid}/giveup")
    def giveup(sid: str):
        return guard(lambda: academy.give_up(store, sid))

    @app.post("/api/learn/s/{sid}/tutor")
    async def ask(sid: str, req: TutorReq):
        throttle(sid + ":tutor", 1.0)
        if req.step not in academy.STEPS:
            raise HTTPException(400, "bad step")
        try:
            return await tutor.reply(store, sid, req.step, req.message)
        except ChallengeError as e:
            raise HTTPException(400, str(e)) from e
