"""Learn mode: sessions, signals, tutor fallback + leak filter, routes."""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from stegoarena import academy
from stegoarena.config import Config, get_config
from stegoarena.hideseek import LEVELS, try_decode
from stegoarena.store import LocalStore

SECRET = "meet at the fountain"


@pytest.mark.parametrize("level", list(LEVELS))
def test_hide_session_round_trips(tmp_path, level):
    store = LocalStore(tmp_path)
    sid = academy.start_hide(store, level, SECRET)
    pub = academy.public(store, sid)
    truth = pub["truth"]
    res = try_decode(store.read_bytes(f"lessons/{sid}/image.png"), truth["spec"], truth["key"])
    assert res["found"] and res["message"] == SECRET
    assert store.read_bytes(f"lessons/{sid}/diff.png")
    sig = academy.session_signals(store, sid)
    assert set(sig) == {"stego", "cover"} and sig["stego"]["row_profile"]["R"]


def test_easy_chi_drops_after_embedding(tmp_path):
    store = LocalStore(tmp_path)
    sid = academy.start_hide(store, "easy", "x " * 50)
    sig = academy.session_signals(store, sid)
    pairs = [(sig["cover"]["chi"][c], sig["stego"]["chi"][c]) for c in "RGB" if sig["cover"]["chi"][c] is not None]
    assert any(s is not None and s < c for c, s in pairs)


def test_practice_hides_truth_until_done(tmp_path):
    store = LocalStore(tmp_path)
    sid = academy.start_practice(store, "medium")
    truth = store.read_json(f"lessons/{sid}/truth.json")
    blob = json.dumps(academy.public(store, sid))
    assert truth["message"] not in blob and (truth["key"] or "\0") not in blob
    assert not academy.check(store, sid, "wrong")["correct"]
    assert academy.check(store, sid, truth["message"])["correct"]
    assert academy.public(store, sid)["reveal"]["message"] == truth["message"]


def tutor_cfg(key=None):
    cfg = Config()
    cfg.anthropic_api_key, cfg.tutor_model = key, "claude-sonnet-5-5"
    return cfg


def test_tutor_canned_hints_in_order_without_key(tmp_path):
    store = LocalStore(tmp_path)
    sid = academy.start_practice(store, "easy")
    truth = store.read_json(f"lessons/{sid}/truth.json")
    hints = academy.LESSONS["easy"]["hints"]["practice"]
    t = academy.Tutor(tutor_cfg())
    for i in range(len(hints)):
        r = asyncio.run(t.reply(store, sid, "practice", "hint please"))
        assert r["source"] == "canned" and r["reply"] == hints[i]
        assert truth["message"] not in r["reply"]


def test_tutor_leak_filter_replaces_reply(tmp_path):
    store = LocalStore(tmp_path)
    sid = academy.start_practice(store, "easy")
    truth = store.read_json(f"lessons/{sid}/truth.json")

    class Fake:
        async def create(self, **kw):
            return NS(stop_reason="end_turn", content=[NS(type="text", text=f"it says {truth['message']}")])

    t = academy.Tutor(tutor_cfg("sk-test"))
    t._client = NS(beta=NS(messages=Fake()))
    r = asyncio.run(t.reply(store, sid, "practice", "tell me"))
    assert r["source"] == "canned" and truth["message"] not in r["reply"]


def test_tutor_uses_claude_reply_when_clean(tmp_path):
    store = LocalStore(tmp_path)
    sid = academy.start_practice(store, "easy")

    class Fake:
        async def create(self, **kw):
            assert kw["model"] == "claude-sonnet-5-5"
            return NS(stop_reason="end_turn", content=[NS(type="text", text="Which bit plane looks noisy?")])

    t = academy.Tutor(tutor_cfg("sk-test"))
    t._client = NS(beta=NS(messages=Fake()))
    assert asyncio.run(t.reply(store, sid, "practice", "help"))["source"] == "claude"


def test_routes_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_ROOT", str(tmp_path))
    from stegoarena.ui.app import create_app

    cfg = get_config()
    cfg.arena_root, cfg.anthropic_api_key = tmp_path, None
    c = TestClient(create_app(cfg))
    assert c.get("/learn").status_code == 200 and c.get("/learn/easy").status_code == 200
    assert set(c.get("/api/learn/lessons").json()["lessons"]) == set(LEVELS)
    p = c.post("/api/learn/easy/practice").json()
    sid = p["meta"]["sid"]
    assert "reveal" not in p and c.get(p["image"]).status_code == 200
    assert c.get(f"/api/learn/s/{sid}/img/cover.png").status_code == 404
    assert c.post(f"/api/learn/s/{sid}/tutor", json={"message": "hi", "step": "practice"}).json()["source"] == "canned"
    h = c.post("/api/learn/hard/hide", json={"message": SECRET}).json()
    assert h["truth"]["message"] == SECRET and c.get(h["diff"]).status_code == 200
    assert c.post("/api/learn/nope/practice").status_code == 400
