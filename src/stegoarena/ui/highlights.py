"""Read-only views over stored rounds for the demo UI: per-attempt metadata and
"highlight" moments (fastest crack, winning branch, Keenable-informed command, ...).

Nothing here runs agents; it only reads result.json / trace.jsonl / summaries."""

from __future__ import annotations

import json
import re

from ..store import ArenaStore

EVAL_ROUND = 900
_RESULT_RE = re.compile(r"^rounds/(\d+)/attempts/([^/]+)/result\.json$")


def is_scripted(finder: str | None) -> bool:
    return (finder or "").startswith("scripted")


def attempt_meta(store: ArenaStore, round_no: int, attempt_ids: list[str]) -> dict[str, dict]:
    """Who ran each attempt and how it went (labels the attempt pills on scheme cards)."""
    out = {}
    for aid in attempt_ids:
        r = store.read_json(f"rounds/{round_no}/attempts/{aid}/result.json") or {}
        out[aid] = {"finder": r.get("finder"), "persona": r.get("persona") or ("scripted" if is_scripted(r.get("finder")) else r.get("finder")),
                    "cracked": bool(r.get("cracked")), "score": r.get("score"), "decoy_submitted": bool(r.get("decoy_submitted")),
                    "elapsed_s": r.get("elapsed_s"), "fork_s": r.get("fork_s"), "branched": bool(r.get("branches")), "searches": len(r.get("searches") or [])}
    return out


def _trace(store: ArenaStore, round_no: int, aid: str) -> list[dict]:
    key = f"rounds/{round_no}/attempts/{aid}/trace.jsonl"
    if not store.exists(key):
        return []
    return [json.loads(x) for x in store.read_bytes(key).decode().splitlines() if x.strip()]


def _first(events: list[dict], pred) -> int | None:
    return next((i for i, e in enumerate(events) if pred(e)), None)


def compute_highlights(store: ArenaStore) -> list[dict]:
    results: list[tuple[int, str, dict]] = []
    for key in store.list("rounds"):
        m = _RESULT_RE.match(key)
        if m:
            r = store.read_json(key)
            if r:
                results.append((int(m.group(1)), m.group(2), r))
    if not results:
        return []
    results.sort(key=lambda t: (t[0] == EVAL_ROUND, t[0], t[1]))  # arena rounds first, eval last

    def tag(rnd: int) -> str:
        return "held-out eval" if rnd == EVAL_ROUND else f"round {rnd}"

    def who(r: dict) -> str:
        return r.get("persona") or r.get("finder") or "finder"

    out: list[dict] = []

    # ⚡ fastest crack by an AI finder
    cracked = [(rnd, aid, r) for rnd, aid, r in results if r.get("cracked") and not is_scripted(r.get("finder")) and r.get("elapsed_s")]
    if cracked:
        rnd, aid, r = min(cracked, key=lambda t: t[2]["elapsed_s"])
        ev = _trace(store, rnd, aid)
        out.append({"kind": "fastest", "icon": "⚡", "title": f"Cracked in {r['elapsed_s']:.0f}s",
                    "blurb": f"The {who(r)} finder recovered the hidden flag fastest ({tag(rnd)}).",
                    "round": rnd, "attempt_id": aid, "event_i": _first(ev, lambda e: e["type"] == "submit")})

    # 🔀 a branch that won
    for rnd, aid, r in results:
        if any(b.get("extracted") for b in (r.get("branches") or [])):
            ev = _trace(store, rnd, aid)
            win = next(b for b in r["branches"] if b.get("extracted"))
            out.append({"kind": "branch", "icon": "🔀", "title": "Parallel hypotheses: one branch won",
                        "blurb": f"The finder forked its live sandbox into {len(r['branches'])} branches; '{win.get('label')}' extracted the flag.",
                        "round": rnd, "attempt_id": aid,
                        "event_i": _first(ev, lambda e: e["type"] == "branch_result") or _first(ev, lambda e: e["type"] == "fork" and e.get("parent_sandbox"))})
            break

    # 🔎 a Keenable search that shaped the next command
    for rnd, aid, r in results:
        if not r.get("searches"):
            continue
        ev = _trace(store, rnd, aid)
        ci = _first(ev, lambda e: e["type"] == "tool_call" and e.get("informed_by"))
        if ci is None:
            continue
        inf = ev[ci]["informed_by"]
        si = _first(ev, lambda e: e["type"] == "search" and e.get("i") == inf.get("search_i"))
        out.append({"kind": "keenable", "icon": "🔎", "title": "A web search changed its next move",
                    "blurb": f"Searched Keenable for “{inf.get('query')}”" + (f" after spotting {inf['trigger']}" if inf.get("trigger") else "") + ", then acted on it.",
                    "round": rnd, "attempt_id": aid, "event_i": si if si is not None else ci})
        break

    # 😈 decoy: the baseline took it, an AI finder didn't
    by_scheme: dict[tuple[int, str], list[tuple[str, dict]]] = {}
    for rnd, aid, r in results:
        by_scheme.setdefault((rnd, r.get("scheme_id")), []).append((aid, r))
    for (rnd, sid), items in by_scheme.items():
        fooled = [r for _, r in items if is_scripted(r.get("finder")) and r.get("decoy_submitted")]
        smart = [(aid, r) for aid, r in items if not is_scripted(r.get("finder")) and r.get("cracked")]
        if fooled and smart:
            aid, r = smart[0]
            ev = _trace(store, rnd, aid)
            out.append({"kind": "decoy", "icon": "😈", "title": "Dodged a decoy",
                        "blurb": f"A fake flag was planted in {sid}. The scripted baseline submitted it; the {who(r)} finder found the real one.",
                        "round": rnd, "attempt_id": aid, "event_i": _first(ev, lambda e: e["type"] == "submit")})
            break

    # 🧰 a tool the agent wrote and kept
    committed: set[str] = set()
    for key in store.list("genomes/finders"):
        if key.endswith("/learning.jsonl"):
            for line in store.read_bytes(key).decode().splitlines():
                if line.strip():
                    committed.update(json.loads(line).get("committed") or [])
    for rnd, aid, r in results:
        names = [n for n in (r.get("pending_tools") or {}) if n in committed]
        if names:
            ev = _trace(store, rnd, aid)
            out.append({"kind": "tool", "icon": "🧰", "title": f"Wrote a tool it kept: {names[0]}",
                        "blurb": "Mid-attempt, the finder saved a reusable script; it passed a sandbox test and now ships into every future fork.",
                        "round": rnd, "attempt_id": aid, "event_i": _first(ev, lambda e: e["type"] == "save_tool" and e.get("name") == names[0])})
            break

    # 🛡 hardest scheme that survived every finder
    best = None
    for r_no in store.read_json("rounds/index.json", {"rounds": []})["rounds"]:
        for n in (store.read_json(f"rounds/{r_no}/summary.json") or {}).get("schemes", []):
            if not n.get("cracked") and n.get("best_attempt") and (best is None or n["difficulty"] > best[1]["difficulty"]):
                best = (r_no, n)
    if best:
        rnd, n = best
        out.append({"kind": "survivor", "icon": "🛡", "title": "The hider's best: nobody cracked it",
                    "blurb": f"{n['scheme_id']} ({n['spec']['technique']}, difficulty {n['difficulty']}) survived every finder in round {rnd}.",
                    "round": rnd, "attempt_id": n["best_attempt"], "event_i": None, "scheme_id": n["scheme_id"]})
    return out
