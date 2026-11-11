"""Demo UI: /guide, attempt metadata on cards, highlight detection, empty states."""

import json

import pytest
from fastapi.testclient import TestClient

from stegoarena.store import LocalStore


def trace(store, rnd, aid, events):
    for i, e in enumerate(events, 1):
        store.append_jsonl(f"rounds/{rnd}/attempts/{aid}/trace.jsonl", {"i": i, "t": float(i), **e})


def result(store, rnd, aid, **r):
    store.write_json(f"rounds/{rnd}/attempts/{aid}/result.json", {"attempt_id": aid, "round": rnd, **r})


@pytest.fixture
def seeded(tmp_path):
    st = LocalStore(tmp_path)
    spec = {"technique": "dct", "channels": ["G"], "bit_plane": 0, "order": "prng", "dct_coeff": "2,1", "dct_delta": 16,
            "palette_colors": 64, "alpha_mode": "alpha_lsb", "cipher": "xor", "key_source": "biglist", "decoy": "trailer"}
    a_fast, a_branch, a_search, a_dumb, a_tool = ("g1-decoy_0-f-statistician-ab12-k0-0001", "g1-decoy_0-f-locksmith-cd34-k0-0002",
                                                  "g1-key_sm0-f-scout-ef56-k0-0003", "g1-decoy_0-scripted-k0-0004", "g1-key_sm0-f-locksmith-cd34-k0-0005")
    st.write_json("rounds/index.json", {"rounds": [1]})
    st.write_json("rounds/1/summary.json", {"round": 1, "provider": "local", "k": 1, "elapsed_s": 50, "finders": [], "hider": "h", "schemes": [
        {"scheme_id": "g1-decoy_0", "spec": spec, "difficulty": 7.5, "cracked": True, "best_score": 1.0, "best_attempt": a_fast,
         "attempts": [a_fast, a_branch, a_dumb], "rationale": "x"},
        {"scheme_id": "g1-key_sm0", "spec": spec, "difficulty": 9.0, "cracked": False, "best_score": 0.35, "best_attempt": a_search,
         "attempts": [a_search, a_tool], "rationale": "y"}]})
    result(st, 1, a_fast, scheme_id="g1-decoy_0", finder="f-statistician-ab12", persona="statistician", cracked=True, score=1.0, elapsed_s=12.5, fork_s=2.1)
    trace(st, 1, a_fast, [{"type": "fork"}, {"type": "tool_call", "tool": "run_shell", "input": {"command": "x"}}, {"type": "submit", "flag": "F"}])
    result(st, 1, a_branch, scheme_id="g1-decoy_0", finder="f-locksmith-cd34", persona="locksmith", cracked=True, score=1.0, elapsed_s=40,
           branches=[{"label": "b0:dct", "extracted": ["F"]}, {"label": "b1:lsb", "extracted": []}])
    trace(st, 1, a_branch, [{"type": "fork"}, {"type": "fork", "parent_sandbox": "p", "lane": "b0:dct"}, {"type": "branch_result", "branches": []}])
    result(st, 1, a_dumb, scheme_id="g1-decoy_0", finder="scripted-v0", cracked=False, score=0.0, decoy_submitted=True, elapsed_s=3)
    trace(st, 1, a_dumb, [{"type": "submit", "flag": "decoy"}])
    result(st, 1, a_search, scheme_id="g1-key_sm0", finder="f-scout-ef56", persona="scout", cracked=False, score=0.35, elapsed_s=88,
           searches=[{"query": "chi square lsb", "trigger": "LSB pairs"}])
    trace(st, 1, a_search, [{"type": "unfamiliar", "signals": ["LSB pairs"]}, {"type": "search", "query": "chi square lsb", "trigger": "LSB pairs"},
                            {"type": "tool_call", "tool": "run_shell", "input": {"command": "y"}, "informed_by": {"query": "chi square lsb", "search_i": 2, "trigger": "LSB pairs"}}])
    result(st, 1, a_tool, scheme_id="g1-key_sm0", finder="f-locksmith-cd34", persona="locksmith", cracked=False, score=0.2, elapsed_s=90,
           pending_tools={"lattice_scan": {"description": "d", "code": "print(1)"}})
    trace(st, 1, a_tool, [{"type": "fork"}, {"type": "save_tool", "name": "lattice_scan"}])
    st.append_jsonl("genomes/finders/f-locksmith-cd34/learning.jsonl", {"generation": 1, "committed": ["lattice_scan"], "rejected": []})
    return st, dict(fast=a_fast, branch=a_branch, search=a_search, dumb=a_dumb, tool=a_tool)


def client_for(root, monkeypatch):
    monkeypatch.setenv("ARENA_ROOT", str(root))
    monkeypatch.setenv("STEGOARENA_PROVIDER", "local")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ARCHIL_DISK_ID", "")
    from stegoarena.config import get_config
    from stegoarena.ui.app import create_app

    return TestClient(create_app(get_config()))


def test_guide_page_and_nav(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as c:
        g = c.get("/guide")
        assert g.status_code == 200 and "How to use StegoArena" in g.text and 'href="/guide" class="on"' in g.text
        for page in ("/", "/hs", "/learn"):
            assert 'href="/guide"' in c.get(page).text, page
        assert "GLOSSARY" in c.get("/static/glossary.js").text


def test_attempt_meta_labels_the_real_finder(seeded, monkeypatch):
    st, a = seeded
    with client_for(st.root, monkeypatch) as c:
        meta = c.get("/api/overview").json()["attempt_meta"]["1"]
    # the old UI labelled this pill "decoy_0" (split on '-'); now it's the finder's persona
    assert meta[a["fast"]]["persona"] == "statistician" and meta[a["fast"]]["cracked"] is True
    assert meta[a["dumb"]]["persona"] == "scripted" and meta[a["dumb"]]["decoy_submitted"] is True
    assert meta[a["branch"]]["branched"] is True and meta[a["search"]]["searches"] == 1


def test_highlights_find_each_moment(seeded, monkeypatch):
    st, a = seeded
    with client_for(st.root, monkeypatch) as c:
        hl = {h["kind"]: h for h in c.get("/api/highlights").json()}
    assert hl["fastest"]["attempt_id"] == a["fast"] and hl["fastest"]["event_i"] == 2  # the submit event
    assert hl["branch"]["attempt_id"] == a["branch"] and hl["branch"]["event_i"] == 2  # branch_result
    assert hl["keenable"]["attempt_id"] == a["search"] and hl["keenable"]["event_i"] == 1  # the search event itself
    assert "chi square lsb" in hl["keenable"]["blurb"]
    assert hl["decoy"]["attempt_id"] in (a["fast"], a["branch"])
    assert hl["tool"]["attempt_id"] == a["tool"] and hl["tool"]["event_i"] == 1
    assert hl["survivor"]["scheme_id"] == "g1-key_sm0"


def test_empty_store_is_friendly(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as c:
        assert c.get("/api/highlights").json() == []
        ov = c.get("/api/overview").json()
        assert ov["rounds"] == [] and ov["attempt_meta"] == {}
        assert c.get("/").status_code == 200
