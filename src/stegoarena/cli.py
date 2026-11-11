"""stegoarena CLI.

  stegoarena smoke     [--provider P] [--finder scripted|llm]   known-easy scheme must be cracked
  stegoarena template  [--provider archil|docker]               build the tool-loaded template
  stegoarena run       --rounds 3 --schemes 4 --k 3 [--finder llm|scripted|both] [--hider llm|ladder]
  stegoarena evolve    --generations 2 [--schemes-per-hider 2]  genome populations: learn, branch, select
  stegoarena eval-set  [--n 8]                                   build the frozen held-out set
  stegoarena eval      --genome static|champion|<gid>            same-model learning-curve point
  stegoarena ui        [--port 8000]                             web UI + replay (reads ./arena_data)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .config import get_config


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="stegoarena")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("smoke", "template", "run", "evolve", "eval-set", "eval"):
        p = sub.add_parser(name)
        p.add_argument("--provider", choices=["local", "docker", "archil"])
        if name == "smoke":
            p.add_argument("--finder", default="scripted", choices=["scripted", "llm"])
        if name == "evolve":
            p.add_argument("--generations", type=int, default=1)
            p.add_argument("--schemes-per-hider", type=int, default=2)
        if name == "eval-set":
            p.add_argument("--n", type=int, default=8)
            p.add_argument("--force", action="store_true")
        if name == "eval":
            p.add_argument("--genome", default="champion", help="static | champion | <genome id>")
            p.add_argument("--label", default=None)
        if name == "run":
            p.add_argument("--rounds", type=int, default=1)
            p.add_argument("--start-round", type=int, default=None, help="default: after the last stored round")
            p.add_argument("--schemes", type=int, default=4)
            p.add_argument("--k", type=int, default=3)
            p.add_argument("--finder", default="llm", choices=["llm", "scripted", "both"])
            p.add_argument("--hider", default="llm", choices=["llm", "ladder"])
    pu = sub.add_parser("ui")
    pu.add_argument("--port", type=int, default=8000)
    pu.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = get_config()
    if getattr(a, "provider", None):
        cfg.provider = a.provider

    if a.cmd == "ui":
        import uvicorn

        from .ui.app import create_app

        uvicorn.run(create_app(cfg), host=a.host, port=a.port)
        return
    asyncio.run(_async_main(a, cfg))


async def _async_main(a, cfg) -> None:
    from .orchestrator import Arena, smoke

    if a.cmd == "smoke":
        r = await smoke(cfg, a.finder)
        print(json.dumps({k: r.get(k) for k in ("finder", "cracked", "score", "submitted_flag", "elapsed_s", "fork_s", "provider", "stop_reason")}, indent=1))
        raise SystemExit(0 if r.get("cracked") else 1)
    if a.cmd == "template":
        from .sandbox.base import make_provider

        prov = make_provider(cfg)
        sb = await prov.create_from_template()
        child = await prov.fork(sb)
        res = await prov.exec(child, "for t in zsteg binwalk exiftool steghide stegseek python3; do command -v $t >/dev/null && echo ok $t || echo MISSING $t; done; python3 -c 'import arena_schemes; print(\"arena_schemes ok\")'")
        print(res.stdout, res.stderr)
        await prov.destroy(child)
        return
    if a.cmd in ("evolve", "eval-set", "eval"):
        from .eval import build_eval_set, run_eval
        from .genome import FinderGenome, static_finder
        from .population import champion_finder, evolve

        arena = Arena(cfg, finder="llm", hider="llm")
        try:
            if a.cmd == "eval-set":
                print(build_eval_set(arena.store, a.n, force=a.force))
            elif a.cmd == "evolve":
                pop = await evolve(arena, a.generations, a.schemes_per_hider)
                h = pop["history"][-1]
                print(f"generation {pop['generation']} ({h['elapsed_s']}s)")
                for k, v in sorted(h["ratings"].items(), key=lambda kv: -kv[1]):
                    print(f"  {k:40} {v:7.1f}")
                for l in h["learned"]:
                    print(f"  learned {l['genome']}: {l['playbook_len']} rules, tools={l['tools']} | {l['insight']}")
                for e in h["selection"]:
                    print(f"  selection: {e['kind']} {e['retired']} retired -> {e['child']} (from {e['parent']}): {e['why']}")
            else:
                if a.genome == "static":
                    g, gen = static_finder(), 0
                elif a.genome == "champion":
                    g = champion_finder(arena.store)
                    gen = arena.store.read_json("population.json")["generation"]
                else:
                    g, gen = FinderGenome.load(arena.store, a.genome), None
                label = a.label or f"{g.id}@gen{gen}"
                r = await run_eval(arena, g, label, gen)
                print(json.dumps({k: r[k] for k in ("label", "crack_rate", "mean_score", "mean_time_to_crack_s", "decoys_taken", "playbook_len", "tools")}, indent=1))
        finally:
            await arena.close()
        return
    if a.cmd == "run":
        finders = ["llm", "scripted"] if a.finder == "both" else [a.finder]
        arena = Arena(cfg, finder=finders[0], hider=a.hider)
        if arena.finder_kind != finders[0]:
            finders = [arena.finder_kind]
        start = a.start_round or (max(arena.store.read_json("rounds/index.json", {"rounds": [0]})["rounds"] or [0]) + 1)
        try:
            for r in range(start, start + a.rounds):
                out = await arena.run_round(r, a.schemes, a.k, finders)
                print(f"\n=== round {r} ({out['elapsed_s']}s, provider={out['provider']}) ===")
                for s in out["schemes"]:
                    print(f"  {s['scheme_id']:8} {s['spec']['technique']:8} diff={s['difficulty']:<5} "
                          f"{'CRACKED' if s['cracked'] else 'survived':9} best={s['best_score']:.2f}  {s['rationale'][:60]}")
        finally:
            await arena.close()


if __name__ == "__main__":
    main()
