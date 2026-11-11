"""Live arena: upload -> hider designs -> embedded in YOUR photo -> finders race -> results (offline)."""

import base64
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from stegoarena.live import LiveError, invisibility, prepare_photo


def png_b64(w=300, h=220):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 255) // w, (y * 255) // h, 120)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_ROOT", str(tmp_path))
    monkeypatch.setenv("STEGOARENA_PROVIDER", "local")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ARCHIL_DISK_ID", "")
    monkeypatch.setenv("KEENABLE_ENABLED", "0")
    from stegoarena.config import get_config
    from stegoarena.ui.app import create_app

    with TestClient(create_app(get_config())) as c:
        yield c


def test_photo_validation():
    with pytest.raises(LiveError):
        prepare_photo(b"not an image")
    small = io.BytesIO()
    Image.new("RGB", (40, 40)).save(small, format="PNG")
    with pytest.raises(LiveError, match="too small"):
        prepare_photo(small.getvalue())
    img, src = prepare_photo(None)
    assert src == "sample" and img.size == (512, 512)


def test_live_match_end_to_end_offline(client):
    assert client.get("/live").status_code == 200
    opts = client.get("/api/live/options").json()
    assert opts["llm"] is False and opts["lineup"] == ["scripted baseline"]
    lid = client.post("/api/live", json={"photo_b64": png_b64()}).json()["lid"]

    since, seen_hider, lane_events, meta = {}, [], {}, None
    for _ in range(120):
        d = client.get(f"/api/live/{lid}/state", params={"since": json.dumps(since)}).json()
        meta = d["meta"]
        seen_hider += d["hider"]
        since["hider"] = len(seen_hider)
        for aid, evs in d["lanes"].items():
            lane_events.setdefault(aid, []).extend(evs)
            since[aid] = len(lane_events[aid])
        if meta["status"] in ("done", "error"):
            break
        time.sleep(0.25)
    assert meta["status"] == "done", meta
    kinds = [e["type"] for e in seen_hider]
    assert kinds[0] == "start" and "design" in kinds and "embedded" in kinds
    emb = next(e for e in seen_hider if e["type"] == "embedded")
    assert emb["max_change"] <= 255 and 0 < emb["pixels_changed_pct"] < 100
    # the scheme was embedded in the uploaded photo (same size), and both images are served
    assert meta["width"] == 300 and meta["source"] == "upload"
    stego = Image.open(io.BytesIO(client.get(f"/api/live/{lid}/stego.png").content))
    assert stego.size == (300, 220)
    assert client.get(f"/api/live/{lid}/original.png").status_code == 200
    (aid,) = [l["aid"] for l in meta["lanes"]]
    assert any(e["type"] == "fork" for e in lane_events[aid]) and any(e["type"] == "scored" for e in lane_events[aid])
    assert len(meta["results"]) == 1 and meta["reveal"]["flag"].startswith("FLAG{")
    assert client.get("/api/live/recent").json()[0]["lid"] == lid
    # live matches stay out of the Lab's round index and Elo
    assert client.get("/api/overview").json()["rounds"] == []


def test_invisibility_measure():
    a = Image.new("RGB", (10, 10), (100, 100, 100))
    b = a.copy()
    b.putpixel((0, 0), (101, 100, 100))
    buf = io.BytesIO()
    b.save(buf, format="PNG")
    p = invisibility(a, buf.getvalue())
    assert p["pixels_changed_pct"] == 1.0 and p["max_change"] == 1


def test_fair_mode_caps_biglist():
    from stegoarena.arena_schemes import validate_spec
    from stegoarena.live import enforce_mode

    sp = validate_spec(dict(technique="lsb", channels=["B"], bit_plane=2, order="prng", cipher="aes", key_source="biglist"))
    fair, capped = enforce_mode(sp, "fair")
    assert capped and fair.key_source == "wordlist" and fair.bit_plane == 2 and fair.cipher == "aes"
    allout, capped = enforce_mode(sp, "allout")
    assert not capped and allout.key_source == "biglist"
