"""Learn mode: guided lessons mirroring the Hide & Seek levels, with an AI tutor.

Steps per lesson: concept -> hide -> signals -> practice -> graduate. Lesson text lives in
lessons.json (content only). Sessions are stored on the shared FS:

    lessons/{sid}/meta.json | truth.json | image.png [| cover.png | diff.png]
    lessons/{sid}/log.jsonl     learner actions (for the tutor)
    lessons/{sid}/tutor.jsonl   tutor conversation

A "hide" session is the learner's own, so everything is visible. A "practice" session's
truth stays server-side until it is solved or given up.
"""

from __future__ import annotations

import io
import json
import logging
import random
import re
import secrets
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .arena_schemes import load_png, validate_spec
from .arena_schemes.triage import chi_by_channel
from .generator.build import WORDLIST
from .hideseek import LEVELS, MAX_MSG, ChallengeError, canon, embed_secret, inspect_png, load_cover, try_decode
from .store import ArenaStore

log = logging.getLogger(__name__)
SID_RE = re.compile(r"^[a-z0-9]{6,16}$")
STEPS = ["concept", "hide", "signals", "practice", "graduate"]
LESSONS: dict[str, dict] = json.loads((Path(__file__).parent / "lessons.json").read_text())


def base(sid: str) -> str:
    if not SID_RE.fullmatch(sid or ""):
        raise ChallengeError("bad session id")
    return f"lessons/{sid}"


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def diff_image(cover: Image.Image, stego: Image.Image) -> Image.Image:
    """White where any channel of a pixel changed, black elsewhere."""
    a = np.asarray(cover.convert("RGB"), dtype=np.int16)
    b = np.asarray(stego.convert("RGB"), dtype=np.int16)
    changed = (a != b).any(axis=2)
    return Image.fromarray((changed * 255).astype(np.uint8), "L")


def lsb_row_profile(img: Image.Image, channel: int = 0, plane: int = 0, rows: int = 128) -> list[float]:
    """Per-row rate at which the chosen bit flips between neighbouring pixels, averaged into <=`rows` bands."""
    bits = (np.asarray(img.convert("RGB"))[:, :, channel] >> plane) & 1
    per_row = (bits[:, 1:] != bits[:, :-1]).mean(axis=1)
    bands = np.array_split(per_row, min(rows, len(per_row)))
    return [round(float(b.mean()), 4) for b in bands]


def signals(png: bytes, cover: bytes | None = None) -> dict:
    def one(data: bytes) -> dict:
        img = load_png(data)
        return {"chi": {k: (round(v, 3) if v is not None else None) for k, v in chi_by_channel(img).items()},
                "file": inspect_png(data),
                "row_profile": {c: lsb_row_profile(img, i) for i, c in enumerate("RGB")}}

    return {"stego": one(png), **({"cover": one(cover)} if cover else {})}


# ------------------------------------------------------------------ sessions
def _check_level(level: str) -> None:
    if level not in LEVELS:
        raise ChallengeError(f"level must be one of {list(LEVELS)}")


def start_hide(store: ArenaStore, level: str, message: str, photo: bytes | None = None) -> str:
    _check_level(level)
    message = " ".join((message or "").split())
    if not 1 <= len(message) <= MAX_MSG or not message.isprintable():
        raise ChallengeError(f"secret must be 1..{MAX_MSG} printable characters")
    spec = validate_spec(dict(LEVELS[level]))
    cover, cover_name = load_cover(photo)
    data, key, decoy, decoy_spec, seed = embed_secret(cover, message, spec)
    sid, b = secrets.token_hex(4), None
    b = base(sid)
    store.write_bytes(f"{b}/image.png", data)
    store.write_bytes(f"{b}/cover.png", _png(cover))
    store.write_bytes(f"{b}/diff.png", _png(diff_image(cover, load_png(data))))
    store.write_json(f"{b}/truth.json", {"message": message, "key": key, "spec": spec.to_dict(), "decoy": decoy, "cover": cover_name})
    store.write_json(f"{b}/meta.json", {"sid": sid, "kind": "hide", "level": level, "created": time.time(), "status": "open"})
    return sid


def start_practice(store: ArenaStore, level: str) -> str:
    _check_level(level)
    message = " ".join(random.sample(WORDLIST, 4))
    spec = validate_spec(dict(LEVELS[level]))
    cover, _ = load_cover(None)
    data, key, decoy, decoy_spec, _seed = embed_secret(cover, message, spec)
    sid = secrets.token_hex(4)
    b = base(sid)
    store.write_bytes(f"{b}/image.png", data)
    store.write_json(f"{b}/truth.json", {"message": message, "key": key, "spec": spec.to_dict(), "decoy": decoy})
    store.write_json(f"{b}/meta.json", {"sid": sid, "kind": "practice", "level": level, "created": time.time(), "status": "open"})
    return sid


def meta(store: ArenaStore, sid: str) -> dict:
    m = store.read_json(f"{base(sid)}/meta.json")
    if m is None:
        raise ChallengeError("no such session")
    return m


def log_action(store: ArenaStore, sid: str, type_: str, **data) -> None:
    store.append_jsonl(f"{base(sid)}/log.jsonl", {"ts": time.time(), "type": type_, **data})


def actions(store: ArenaStore, sid: str, n: int = 12) -> list[dict]:
    key = f"{base(sid)}/log.jsonl"
    if not store.exists(key):
        return []
    return [json.loads(x) for x in store.read_bytes(key).decode().splitlines() if x.strip()][-n:]


def public(store: ArenaStore, sid: str) -> dict:
    m = meta(store, sid)
    out = {"meta": m, "steps": STEPS, "lesson": LESSONS[m["level"]], "image": f"/api/learn/s/{sid}/img/image.png"}
    truth = store.read_json(f"{base(sid)}/truth.json")
    if m["kind"] == "hide":
        out.update(cover=f"/api/learn/s/{sid}/img/cover.png", diff=f"/api/learn/s/{sid}/img/diff.png", truth=truth)
    elif m["status"] != "open":
        out["reveal"] = {"message": truth["message"], "key": truth.get("key"), "spec": truth["spec"]}
    return out


def session_signals(store: ArenaStore, sid: str) -> dict:
    m, b = meta(store, sid), base(sid)
    cover = store.read_bytes(f"{b}/cover.png") if m["kind"] == "hide" else None
    return signals(store.read_bytes(f"{b}/image.png"), cover)


def try_(store: ArenaStore, sid: str, spec: dict, key: str | None, wordlist: bool) -> dict:
    m = meta(store, sid)
    if m["kind"] != "practice":
        raise ChallengeError("trying a decode only applies to practice images")
    res = try_decode(store.read_bytes(f"{base(sid)}/image.png"), spec, key, wordlist)
    log_action(store, sid, "try", technique=spec.get("technique"), channels=spec.get("channels"), bit_plane=spec.get("bit_plane"),
               order=spec.get("order"), cipher=spec.get("cipher"), key=key, wordlist=wordlist, found=res.get("found"))
    return res


def check(store: ArenaStore, sid: str, message: str) -> dict:
    m = meta(store, sid)
    if m["kind"] != "practice":
        raise ChallengeError("only practice images take answers")
    truth = store.read_json(f"{base(sid)}/truth.json")
    ok = canon(message) == canon(truth["message"])
    decoy = bool(truth.get("decoy")) and canon(message) == canon(truth["decoy"])
    log_action(store, sid, "answer", message=message[:MAX_MSG], correct=ok, decoy=decoy)
    if ok and m["status"] == "open":
        store.write_json(f"{base(sid)}/meta.json", {**m, "status": "solved"})
    return {"correct": ok, "decoy": decoy}


def give_up(store: ArenaStore, sid: str) -> dict:
    m = meta(store, sid)
    if m["kind"] == "practice" and m["status"] == "open":
        store.write_json(f"{base(sid)}/meta.json", {**m, "status": "gave_up"})
        log_action(store, sid, "give_up")
    return public(store, sid)


# --------------------------------------------------------------------- tutor
TUTOR_SYSTEM = """You are a patient steganography tutor inside a learning app. The learner is working through the "{level}" lesson, currently on the step "{step}".

Lesson material (the lesson's own content; build on it, don't contradict it):
{lesson}

Practice image facts (private to you): technique={technique}, channels={channels}, bit_plane={bit_plane}, order={order}, cipher={cipher}, key_source={key_source}.

Learner's recent actions: {recent}

Rules: teach Socratically. Give one nudge at a time, make each repeated ask a little more concrete than the last, and explain why a signal appears. Never state the hidden message or the key itself, and don't spell out exact parameter values until the learner has found the technique. Keep replies under 120 words."""


def _leaks(reply: str, truth: dict) -> bool:
    r = canon(reply)
    if truth["message"] and canon(truth["message"]) in r:
        return True
    key = (truth.get("key") or "").casefold()
    return len(key) >= 3 and key in reply.casefold()


class Tutor:
    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.cfg.anthropic_api_key, max_retries=2)
        return self._client

    def canned(self, store: ArenaStore, sid: str, step: str, n: int) -> str:
        hints = LESSONS[meta(store, sid)["level"]].get("hints", {}).get(step) or ["Take another look at the signals for this step."]
        return hints[min(n, len(hints) - 1)]

    async def reply(self, store: ArenaStore, sid: str, step: str, message: str) -> dict:
        m, b = meta(store, sid), base(sid)
        key = f"{b}/tutor.jsonl"
        history = [json.loads(x) for x in store.read_bytes(key).decode().splitlines() if x.strip()] if store.exists(key) else []
        asked = sum(1 for h in history if h["role"] == "user" and h.get("step") == step)
        truth = store.read_json(f"{b}/truth.json")
        store.append_jsonl(key, {"role": "user", "step": step, "content": message[:600]})
        text, source = None, "canned"
        if self.cfg.anthropic_api_key:
            try:
                text = await self._ask(m, truth, step, history, message, actions(store, sid))
                source = "claude"
            except Exception as e:  # network, quota, refusal: fall back to scripted hints
                log.warning("tutor call failed: %s", e)
        if text and m["kind"] == "practice" and _leaks(text, truth):
            text, source = None, "canned"
        if not text:
            text, source = self.canned(store, sid, step, asked), "canned"
        store.append_jsonl(key, {"role": "assistant", "step": step, "content": text, "source": source})
        return {"reply": text, "source": source}

    async def _ask(self, m: dict, truth: dict, step: str, history: list, message: str, recent: list) -> str | None:
        s = truth["spec"]
        system = TUTOR_SYSTEM.format(level=m["level"], step=step, lesson=json.dumps(LESSONS[m["level"]])[:4000],
                                     technique=s["technique"], channels=s["channels"], bit_plane=s["bit_plane"], order=s["order"],
                                     cipher=s["cipher"], key_source=s["key_source"], recent=json.dumps(recent)[:1500])
        msgs = [{"role": h["role"], "content": h["content"]} for h in history[-8:]]
        msgs.append({"role": "user", "content": message[:600]})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        resp = await self.client.beta.messages.create(
            model=self.cfg.tutor_model, max_tokens=1200, system=system, messages=msgs,
            output_config={"effort": "low"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if resp.stop_reason == "refusal":
            return None
        return next((b.text.strip() for b in resp.content if b.type == "text" and b.text.strip()), None)
