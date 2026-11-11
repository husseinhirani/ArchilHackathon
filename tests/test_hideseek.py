"""Hide & Seek: build per level, human tools, race flow, and no truth leak before the race ends."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from stegoarena.config import get_config
from stegoarena.hideseek import LEVELS, build_challenge, canon, inspect_png, try_decode
from stegoarena.store import LocalStore

SECRET = "meet at the fountain, 7pm!"


@pytest.mark.parametrize("level", list(LEVELS))
def test_build_and_decode_each_level(tmp_path, level):
    store = LocalStore(tmp_path)
    cid = build_challenge(store, None, SECRET, level, hint="look closely")
    truth = store.read_json(f"challenges/{cid}/truth.json")
    spec = store.read_json(f"challenges/{cid}/spec.json")
    data = store.read_bytes(f"challenges/{cid}/image.png")
    res = try_decode(data, spec, truth["key"])
    assert res["found"] and res["message"] == SECRET
    if level == "hard":
        assert try_decode(data, spec, None, use_wordlist=True)["found"]
    if level == "medium":
        assert inspect_png(data)["text"].get("Comment") == truth["key"]


def test_pixel_decoy_level(tmp_path, monkeypatch):
    import stegoarena.hideseek as hsm

    monkeypatch.setitem(hsm.LEVELS, "decoy", dict(LEVELS["hard"], decoy="pixel"))
    monkeypatch.setitem(hsm.LEVEL_BLURB, "decoy", "test")
    store = LocalStore(tmp_path)
    cid = build_challenge(store, None, SECRET, "decoy")
    truth = store.read_json(f"challenges/{cid}/truth.json")
    data = store.read_bytes(f"challenges/{cid}/image.png")
    spec = store.read_json(f"challenges/{cid}/spec.json")
    assert try_decode(data, spec, truth["key"])["message"] == SECRET
    fake = try_decode(data, truth["decoy_spec"], truth["key"])
    assert fake["found"] and canon(fake["message"]) == canon(truth["decoy_flag"]) != canon(SECRET)


def test_redact():
    from stegoarena.ui.hideseek_routes import redact

    out = redact({"output": '{"flag": "HS1:meet at the fountain", "where": "EXTRACTED"}', "x": ["HS1:abc def"]})
    assert "fountain" not in str(out) and "abc" not in str(out)


def test_canon():
    assert canon("HS1:Meet  at the fountain, 7PM! ") == canon(SECRET)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_ROOT", str(tmp_path))
    monkeypatch.setenv("STEGOARENA_PROVIDER", "local")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ARCHIL_DISK_ID", "")
    cfg = get_config()
    from stegoarena.ui.app import create_app

    with TestClient(create_app(cfg)) as c:
        yield c


def test_race_flow_and_no_leak(client):
    cid = client.post("/api/c", json={"message": SECRET, "level": "easy"}).json()["cid"]
    blob = json.dumps(client.get(f"/api/c/{cid}").json())
    assert "fountain" not in blob and "reveal" not in blob
    assert client.post(f"/api/c/{cid}/start").status_code == 200
    blob = json.dumps(client.get(f"/api/c/{cid}/state").json())
    assert "fountain" not in blob
    spec = {"technique": "lsb", "channels": ["R", "G", "B"], "bit_plane": 0, "order": "sequential", "cipher": "none"}
    assert client.post(f"/api/c/{cid}/try", json={"spec": spec}).json()["message"] == SECRET
    g = client.post(f"/api/c/{cid}/guess", json={"message": "meet at the FOUNTAIN, 7pm!"}).json()
    assert g["correct"]
    for _ in range(100):  # scripted AI finishes in the background
        st = client.get(f"/api/c/{cid}/state").json()
        if st["race"].get("ai_done"):
            break
        time.sleep(0.2)
    assert st["race"]["status"] == "done" and st["race"]["winner"] in ("human", "ai")
    assert st["reveal"]["message"] == SECRET
    assert any(e["type"] == "fork" for e in st["ai"])
    assert client.get("/api/hs/stats").json()["easy"]["n"] == 1
