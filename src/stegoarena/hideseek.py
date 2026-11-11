"""Hide & Seek: the consumer front door on top of the arena.

A user (the hider) uploads a photo, types a secret and picks a level. A friend (the
human seeker) races the AI finder on the same image. The AI runs in one fork of the
tool template, exactly like an arena attempt, and both traces live on the shared FS:

    challenges/{cid}/image.png | spec.json | truth.json | meta.json | race.json
    challenges/{cid}/ai/trace.jsonl | commands.log | result.json
    challenges/{cid}/human/trace.jsonl

truth.json is read only by the server (race scoring, /try, /guess) and is revealed to
clients only once the race is over.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import secrets
import statistics
import time
import zlib

from PIL import Image

from .arena_schemes.decoys import pixel_decoy_spec
from .arena_schemes import MSG_PREFIX, SchemeSpec, difficulty, embed, extract, load_png, save_png, validate_spec
from .finder.scripted import run_scripted
from .finder.trace import Trace
from .generator.build import WORDLIST, GenerationError, make_key
from .generator.covers import make_cover
from .hider.ladder import LADDER
from .scorer import score_attempt
from .store import ArenaStore

log = logging.getLogger(__name__)

LEVELS: dict[str, dict] = {
    "easy": dict(LADDER[0]),    # lsb RGB plane 0, sequential: visible in the bit-plane view
    "medium": dict(LADDER[4]),  # lsb R,G + xor, key sits in PNG metadata
    "hard": dict(LADDER[7]),    # lsb G, prng order + aes, key is a wordlist word
}
LEVEL_BLURB = {
    "easy": "Plain least-significant-bit hiding. Look at the bit planes.",
    "medium": "Encrypted, but the key is hiding in plain sight.",
    "hard": "Scrambled and encrypted. The key is one word from the wordlist.",
}
DECOYS = ["the treasure is under the oak tree", "password is hunter2", "nothing to see here", "meet me at noon",
          "the eagle has landed", "check the other photo"]
MAX_MSG = 120
CID_RE = re.compile(r"^[a-z0-9]{6,16}$")


class ChallengeError(ValueError):
    pass


def canon(s: str | None) -> str:
    """Normalise a secret for comparison: drop the HS1: prefix, case and extra whitespace."""
    s = (s or "").strip()
    if s.startswith(MSG_PREFIX):
        s = s[len(MSG_PREFIX):]
    return " ".join(s.split()).casefold()


def base(cid: str) -> str:
    if not CID_RE.fullmatch(cid or ""):
        raise ChallengeError("bad challenge id")
    return f"challenges/{cid}"


class ChallengeTrace(Trace):
    """Same JSONL trace format as arena attempts, rooted under a challenge."""

    def __init__(self, store: ArenaStore, prefix: str):
        super().__init__(store, 0, "hs")
        self._prefix = prefix

    @property
    def base(self) -> str:  # type: ignore[override]
        return self._prefix


# --------------------------------------------------------------------- build
def build_challenge(store: ArenaStore, photo: bytes | None, message: str, level: str, hint: str = "",
                    *, cid: str | None = None, retries: int = 3) -> str:
    message = " ".join((message or "").split())
    if not 1 <= len(message) <= MAX_MSG or not message.isprintable():
        raise ChallengeError(f"secret must be 1..{MAX_MSG} printable characters")
    if level not in LEVELS:
        raise ChallengeError(f"level must be one of {list(LEVELS)}")
    hint = " ".join((hint or "").split())[:200]
    cid = cid or secrets.token_hex(4)
    spec = validate_spec(dict(LEVELS[level]))
    if photo:
        try:
            cover = Image.open(io.BytesIO(photo)).convert("RGB")
        except Exception as e:
            raise ChallengeError(f"could not read image: {e}") from e
        cover.thumbnail((512, 512))
        cover_name = "upload"
    else:
        cover, cover_name = make_cover(random.randrange(1 << 30), 512, grain=0.0), "procedural"  # no visible grain
    payload = MSG_PREFIX + message
    last: Exception | None = None
    for attempt in range(retries):
        seed = secrets.randbits(31)
        key = make_key(spec, seed)
        decoy = MSG_PREFIX + random.choice(DECOYS) if spec.decoy != "none" else None
        decoy_spec = pixel_decoy_spec(spec, random.Random(seed)) if spec.decoy == "pixel" else None
        try:
            img = embed(cover, decoy, decoy_spec, key) if decoy_spec else cover  # decoy first, like build_scheme
            data = save_png(embed(img, payload, spec, key), spec, key, decoy)
            back = load_png(data)
            if extract(back, spec, key) != payload:
                raise GenerationError("round-trip mismatch")
            if decoy_spec and extract(back, decoy_spec, key) != decoy:
                raise GenerationError("pixel decoy round-trip mismatch")
        except Exception as e:
            last = e
            continue
        b = base(cid)
        store.write_bytes(f"{b}/image.png", data)
        store.write_json(f"{b}/spec.json", {"level": level, "difficulty": difficulty(spec), **spec.to_dict()})
        store.write_json(f"{b}/truth.json", {"flag": payload, "message": message, "key": key, "decoy_flag": decoy,
                                             "decoy_spec": decoy_spec.to_dict() if decoy_spec else None, "cover": cover_name, "seed": seed})
        store.write_json(f"{b}/meta.json", {"cid": cid, "level": level, "hint": hint, "blurb": LEVEL_BLURB[level],
                                            "created": time.time(), "width": cover.width, "height": cover.height})
        store.write_json(f"{b}/race.json", {"status": "waiting"})
        return cid
    raise ChallengeError(f"could not embed the secret in this photo: {last}")


# ---------------------------------------------------------------- human tools
def inspect_png(data: bytes) -> dict:
    """What exiftool/strings would show: text chunks and bytes after IEND (the AI sees the same)."""
    chunks, text, pos = [], {}, 8
    while pos + 8 <= len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8].decode("latin-1")
        body = data[pos + 8:pos + 8 + n]
        chunks.append({"type": typ, "length": n})
        if typ == "tEXt" and b"\0" in body:
            k, v = body.split(b"\0", 1)
            text[k.decode("latin-1")] = v.decode("latin-1")
        elif typ == "zTXt" and b"\0" in body:
            k, v = body.split(b"\0", 1)
            try:
                text[k.decode("latin-1")] = zlib.decompress(v[1:]).decode("latin-1")
            except Exception:
                pass
        pos += 12 + n
        if typ == "IEND":
            break
    trailer = data[pos:]
    return {"chunks": chunks, "text": text, "trailer": trailer[:400].decode("latin-1", errors="replace"),
            "trailer_bytes": len(trailer), "size": len(data)}


HUMAN_TECHNIQUES = ("lsb", "alpha", "transparent", "palette", "dct")
RGB_SETS = [["R", "G", "B"], ["R"], ["G"], ["B"], ["R", "G"], ["R", "B"], ["G", "B"]]
CIPHER_LABEL = {"xor": "XOR", "aes": "AES"}


def human_spec(spec: dict, img_mode: str, key: str | None, use_wordlist: bool) -> dict:
    """Validate the PLAYER's decode form and map it to a scheme spec.

    Rejects every combination the decoder can't meaningfully run, with player-facing text
    (ChallengeError). The agent path never goes through here; it keeps using SchemeSpec directly.
    UI technique names: lsb | alpha (A-channel LSB) | transparent (RGB of fully transparent
    pixels) | palette (index LSB) | dct (one colour channel's block-DCT coefficient)."""
    tech = spec.get("technique")
    alpha_mode = spec.get("alpha_mode")
    if tech == "alpha" and alpha_mode == "transparent_rgb":  # accept the raw agent-style form too
        tech = "transparent"
    if tech not in HUMAN_TECHNIQUES:
        raise ChallengeError("Pick a technique from the list.")
    chans = [str(c).upper() for c in (spec.get("channels") or [])]
    order = spec.get("order") or "sequential"
    cipher = spec.get("cipher") or "none"
    if order not in ("sequential", "prng"):
        raise ChallengeError("Order must be sequential or PRNG.")
    if cipher not in ("none", "xor", "aes"):
        raise ChallengeError("Cipher must be none, XOR or AES.")
    out = {"order": order, "cipher": cipher}

    if tech == "lsb":
        if "A" in chans:
            raise ChallengeError("LSB reads colour channels. To read the A channel, pick the Alpha technique.")
        if sorted(chans, key="RGB".index) not in RGB_SETS:
            raise ChallengeError("Pick one or more colour channels (R, G, B).")
        bp = spec.get("bit_plane", 0)
        if bp not in (0, 1, 2):
            raise ChallengeError("Bit plane must be 0, 1 or 2.")
        out.update(technique="lsb", channels=chans, bit_plane=bp)
    elif tech in ("alpha", "transparent"):
        if img_mode != "RGBA":
            raise ChallengeError("This image has no alpha (transparency) channel, so there's nothing hidden there.")
        if tech == "alpha" and chans not in ([], ["A"]):
            raise ChallengeError("Alpha hiding reads the A channel only. Set Channels to A.")
        out.update(technique="alpha", alpha_mode="alpha_lsb" if tech == "alpha" else "transparent_rgb")
    elif tech == "palette":
        if img_mode != "P":
            raise ChallengeError("This image isn't palette-based (indexed colour), so palette decoding can't apply.")
        out.update(technique="palette")
    else:  # dct
        if len(chans) != 1 or chans[0] not in "RGB":
            raise ChallengeError("DCT reads a single colour channel. Pick R, G or B.")
        coeff, delta = str(spec.get("dct_coeff") or "2,1"), spec.get("dct_delta", 16)
        if coeff not in ("1,2", "2,1", "2,2", "3,1", "1,3", "3,2", "2,3"):
            raise ChallengeError("Pick a DCT coefficient from the list.")
        if not isinstance(delta, int) or not 12 <= delta <= 32:
            raise ChallengeError("DCT step must be a whole number from 12 to 32.")
        out.update(technique="dct", channels=chans, dct_coeff=coeff, dct_delta=delta)

    needs_key = order == "prng" or cipher != "none"
    if needs_key and not (key or use_wordlist):
        if cipher != "none":
            raise ChallengeError(f"{CIPHER_LABEL[cipher]} needs a key. Enter one or click Try every wordlist key.")
        raise ChallengeError("PRNG order needs a key (the seed). Enter one or click Try every wordlist key.")
    if use_wordlist and not needs_key:
        raise ChallengeError("These settings don't use a key (no cipher, sequential order), so the wordlist won't change anything.")
    out["key_source"] = "metadata" if needs_key else "none"
    return out


def try_decode(data: bytes, spec: dict, key: str | None, use_wordlist: bool = False) -> dict:
    """Run the real extract() with the human's guessed parameters (player-facing errors only)."""
    img = load_png(data)
    key = (key or "").strip() or None
    try:
        s = SchemeSpec.from_dict(human_spec(spec, img.mode, key, use_wordlist))
    except ChallengeError as e:
        return {"found": False, "error": str(e)}
    except Exception as e:  # internal detail stays in the server log
        log.info("try_decode rejected spec %s: %s", spec, e)
        return {"found": False, "error": "Those settings can't be decoded. Check the technique and channels."}
    keys = list(WORDLIST) if use_wordlist else [key]
    for k in keys:
        out = extract(img, s, k)
        if out:
            return {"found": True, "message": out[len(MSG_PREFIX):] if out.startswith(MSG_PREFIX) else out, "key": k,
                    "tried": keys.index(k) + 1}
    return {"found": False, "tried": len(keys)}


# ----------------------------------------------------------------------- race
class HideSeek:
    """Owns races. Reuses the Arena's store, sandbox provider, template and search."""

    def __init__(self, arena):
        self.arena = arena
        self.store: ArenaStore = arena.store
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}
        self._modes: dict[str, str] = {}

    def race(self, cid: str) -> dict:
        return self.store.read_json(f"{base(cid)}/race.json", {"status": "waiting"})

    async def _update_race(self, cid: str, **fields) -> dict:
        async with self._lock:
            r = self.race(cid)
            for k, v in fields.items():
                if k == "winner" and r.get("winner"):
                    continue  # first finisher wins
                r[k] = v
            if r.get("winner") or (r.get("ai_done") and r.get("human_gave_up")):
                r["status"] = "done"
            self.store.write_json(f"{base(cid)}/race.json", r)
            return r

    async def start(self, cid: str, *, human: bool = True) -> dict:
        b = base(cid)
        if not self.store.exists(f"{b}/truth.json"):
            raise ChallengeError("no such challenge")
        r = self.race(cid)
        if r.get("status") != "waiting":
            return r
        r = await self._update_race(cid, status="racing", started=time.time(), human=human)
        self._tasks[cid] = asyncio.create_task(self.race_ai(cid))
        return r

    def human_event(self, cid: str, type_: str, **data) -> None:
        r = self.race(cid)
        t = ChallengeTrace(self.store, f"{base(cid)}/human")
        t.t0 = r.get("started") or time.time()
        t.events = len(self.events(cid, "human", 0))
        t.log(type_, **data)

    def events(self, cid: str, who: str, since: int) -> list[dict]:
        import json

        key = f"{base(cid)}/{who}/trace.jsonl"
        if not self.store.exists(key):
            return []
        lines = self.store.read_bytes(key).decode().splitlines()
        return [json.loads(x) for x in lines[since:] if x.strip()]

    async def guess(self, cid: str, message: str) -> dict:
        truth = self.store.read_json(f"{base(cid)}/truth.json")
        r = self.race(cid)
        elapsed = round(time.time() - (r.get("started") or time.time()), 2)
        ok = canon(message) == canon(truth["flag"])
        decoy = bool(truth.get("decoy_flag")) and canon(message) == canon(truth["decoy_flag"])
        self.human_event(cid, "guess", message=message[:MAX_MSG], correct=ok, decoy=decoy)
        if ok:
            r = await self._update_race(cid, human_solved_s=elapsed, winner=r.get("winner") or "human")
        return {"correct": ok, "decoy": decoy, "elapsed_s": elapsed, "race": r}

    async def race_ai(self, cid: str, finder: str | None = None) -> dict:
        arena, b = self.arena, base(cid)
        finder = finder or arena.finder_kind
        meta = self.store.read_json(f"{b}/meta.json")
        spec = self.store.read_json(f"{b}/spec.json")
        truth = self.store.read_json(f"{b}/truth.json")
        trace = ChallengeTrace(self.store, f"{b}/ai")
        sb, fork_s = None, None
        async with arena.sem:
            trace.t0 = self.race(cid).get("started") or time.time()
            t_fork = time.monotonic()
            try:
                sb = await arena.provider.fork(await arena.template(), name=f"hs-{cid}-{secrets.token_hex(2)}")
                fork_s = round(time.monotonic() - t_fork, 3)
                trace.log("fork", sandbox=sb.id, provider=sb.provider, fork_s=fork_s)
                await arena.provider.write_bundle(sb, {"challenge.png": self.store.read_bytes(f"{b}/image.png")})
                if finder == "llm":
                    result = await HSFinder(arena.cfg, arena.search, hint=meta.get("hint", "")).run(arena.provider, sb, trace)
                else:
                    result = await run_scripted(arena.provider, sb, trace)
            except Exception as e:
                log.exception("hide&seek race %s crashed", cid)
                trace.log("error", message=f"AI attempt crashed: {e}")
                result = {"finder": finder, "submitted_flag": None, "signals": [], "stop_reason": "crash"}
            finally:
                if sb is not None:
                    await arena.provider.destroy(sb)
        sub = result.get("submitted_flag")
        for cand in (truth["flag"], truth.get("decoy_flag")):  # tolerate "msg" vs "HS1:msg", case, spacing
            if cand and sub and canon(sub) == canon(cand):
                result["submitted_flag"] = cand
        result.update(cid=cid, level=meta["level"], sandbox=getattr(sb, "id", None), provider=getattr(sb, "provider", None),
                      fork_s=fork_s, **score_attempt(result, spec, truth))
        elapsed = round(time.time() - trace.t0, 2)
        result["race_s"] = elapsed
        trace.log("scored", score=result["score"], cracked=result["cracked"], components=result["components"])
        trace.result(result)
        await self._update_race(cid, ai_done=True, ai_cracked=result["cracked"], ai_score=result["score"],
                                ai_solved_s=elapsed if result["cracked"] else None,
                                **({"winner": "ai"} if result["cracked"] else {}),
                                **({} if self.race(cid).get("human") else {"human_gave_up": True}))
        return result

    async def give_up(self, cid: str) -> dict:
        self.human_event(cid, "give_up")
        return await self._update_race(cid, human_gave_up=True)

    def public(self, cid: str) -> dict:
        """Everything a seeker may see. Truth only after the race is over."""
        b = base(cid)
        meta = self.store.read_json(f"{b}/meta.json")
        if meta is None:
            raise ChallengeError("no such challenge")
        r = self.race(cid)
        out = {"meta": meta, "race": r, "image": f"/hs/img/{cid}.png", "image_mode": self.image_mode(cid),
               "wordlist_size": len(WORDLIST)}
        if r.get("status") == "done":
            truth = self.store.read_json(f"{b}/truth.json")
            out["reveal"] = {"message": truth["message"], "key": truth.get("key"), "decoy": truth.get("decoy_flag"),
                             "spec": self.store.read_json(f"{b}/spec.json")}
        return out

    def image_mode(self, cid: str) -> str:
        """RGB / RGBA / P - same as `file`/exiftool would tell any seeker."""
        if cid not in self._modes:
            self._modes[cid] = Image.open(io.BytesIO(self.store.read_bytes(f"{base(cid)}/image.png"))).mode
        return self._modes[cid]

    def stats(self) -> dict:
        per: dict[str, list[dict]] = {lv: [] for lv in LEVELS}
        humans: dict[str, list[float]] = {lv: [] for lv in LEVELS}
        for key in self.store.list("challenges"):
            if key.endswith("/ai/result.json"):
                res = self.store.read_json(key)
                if res and res.get("level") in per:
                    per[res["level"]].append(res)
            elif key.endswith("/race.json"):
                r = self.store.read_json(key) or {}
                lv = (self.store.read_json(key.replace("race.json", "meta.json")) or {}).get("level")
                if lv in humans and r.get("human_solved_s"):
                    humans[lv].append(r["human_solved_s"])
        out = {}
        for lv, rs in per.items():
            cracked = [r for r in rs if r.get("cracked")]
            times = [r.get("race_s") or r.get("elapsed_s") or 0 for r in cracked]
            out[lv] = {"n": len(rs), "ai_crack_rate": round(len(cracked) / len(rs), 3) if rs else None,
                       "ai_median_s": round(statistics.median(times), 1) if times else None,
                       "humans_solved": len(humans[lv]),
                       "human_median_s": round(statistics.median(humans[lv]), 1) if humans[lv] else None}
        return out

    async def seed(self, per_level: int, finder: str | None = None) -> dict:
        words = WORDLIST
        jobs = []
        for lv in LEVELS:
            for _ in range(per_level):
                msg = " ".join(random.sample(words, 4))
                cid = build_challenge(self.store, None, msg, lv)
                await self._update_race(cid, status="racing", started=time.time(), human=False, seeded=True)
                jobs.append(self.race_ai(cid, finder))
        await asyncio.gather(*jobs)
        return self.stats()


def _hs_finder_cls():
    from .finder.agent import ClaudeFinder

    class _HSFinder(ClaudeFinder):
        def __init__(self, cfg, search, hint: str = ""):
            super().__init__(cfg, search)
            self.hint = hint

        def system(self, budget: float) -> str:
            s = super().system(budget)
            s += ("\n\nHIDE & SEEK MODE: the hidden payload is a free-text message embedded as `HS1:<message>` "
                  "(extract() / triage return it with the prefix). Submit the message via submit_flag (prefix optional). "
                  "Decoys use the same prefix, so only trust a message you actually EXTRACTED.")
            if self.hint:
                s += f"\nThe hider left this hint (the human racing you sees it too): {self.hint!r}"
            return s

    return _HSFinder


def HSFinder(cfg, search, hint: str = ""):  # noqa: N802 - lazy import keeps anthropic optional
    return _hs_finder_cls()(cfg, search, hint=hint)
