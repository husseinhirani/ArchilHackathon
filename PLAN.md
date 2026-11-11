# StegoArena: implementation plan (solo, 4 hours)

A hider agent designs image-stego schemes, a finder agent cracks them blind in forked Archil sandboxes, and both climb an Elo ladder. Demo = **replay of pre-validated logged rounds** + at most one capped live round.

## Status: scaffold done (38 tests green, offline end to end)

```
uv sync && uv run pytest -q                       # 38 passed, no keys needed
uv run stegoarena smoke                           # known-easy scheme, scripted finder, must print cracked=true
uv run stegoarena run --rounds 2 --schemes 5 --k 2 --finder scripted --hider ladder
uv run stegoarena ui                              # http://127.0.0.1:8000
```

## Verified SDK facts (from installed packages, not memory)

| | API | Consequence |
|---|---|---|
| **Archil** `archil==0.21.0` | `Archil(api_key, region)`; `client.sandboxes.create(.aio)(name, base_image, max_ttl_seconds, idle_ttl_seconds, ...)`; `sb.exec(cmd, cwd, env, timeout_seconds)` returns `SandboxProcessResult(status, stdout, stderr, exit_code)`; `sb.fork(name, wait)`, `start/stop/delete`; `sb.files.upload_file/download_file`; `disk.put_object/get_object/append_object/list_objects` | A sandbox **with mounted disks can't be forked while running**, so the template has no mounts and the shared FS is an Archil disk written host-side via the object API (`MirrorStore`). |
| Archil Free tier | 5 concurrent sandboxes, 30-min sessions, egress allowlisted (pkg registries, git hosts, model APIs) | **The finder's LLM loop runs host-side** and only `run_shell` executes in the fork. API keys never enter the sandbox. Keep `MAX_CONCURRENCY=4`. |
| **Keenable** `keenable` SDK | `AsyncKeenable(api_key=None)` `.search(q, max_results)` returns `.results[].title/url/snippet`; `.fetch(url)` returns `.title/.content` | Works keyless (1k req/h). `FallbackSearch` drops to an offline stub per call on error. |
| **Anthropic** `anthropic==2.7.0` | `client.beta.messages.create(..., thinking={"type":"adaptive","display":"summarized"}, output_config={"effort":...}, cache_control=..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`; `beta.messages.parse(output_format=PydanticModel)` | Finder `claude-sonnet-5-5` (effort medium, summarized thinking goes into the replay trace). Hider `claude-opus-5-5` with structured outputs. Server-side refusal fallbacks are **on** for both. |

## Architecture (as built)

```
src/stegoarena/
  arena_schemes/      A  pure numpy/Pillow/cryptography; copied into every sandbox (Kerckhoffs)
    spec.py             flat spec + validation (technique, channels, bit_plane, order, dct_*, palette_colors,
                        alpha_mode, cipher, key_source, decoy)
    techniques.py       LSB | DCT-QIM (8x8 block, one coeff) | palette index-LSB (luminance-sorted) | alpha (lsb / transparent_rgb)
    bits.py             framing, keyed PRNG visit order, xor/aes-ctr ciphers
    api.py              embed / extract / save_png (metadata key, decoys) / difficulty
    triage.py           generic sweep CLI: baseline finder + a tool the LLM finder can call
    wordlist.txt        key space for key_source=wordlist (finder has it too)
  generator/          A  covers (procedural, or ./covers/*.jpg) + build_scheme with round-trip check
  sandbox/            B  SandboxProvider: LocalProvider | DockerProvider | ArchilProvider + setup_template.sh
  finder/             C  agent.py (Claude tool loop, 90s wall clock), scripted.py (finder-v0), search.py, trace.py
  hider/                 agent.py (Opus + Keenable research + structured outputs), ladder.py (curated + mutate)
  scorer.py           D  partial credit (detection .15 / technique .20 / params .20 / flag .45, decoy -.15) + Elo
  orchestrator.py     D  rounds: propose, build, K forks per scheme in parallel, score, Elo, lineage
  store.py            D  LocalStore | ArchilDiskStore | MirrorStore (UI always reads local)
  ui/                 D  FastAPI + one HTML page: Elo chart, ladder, round cards, step-through replay
```

Key design choices worth saying out loud to judges:
- **Kerckhoffs's principle.** The finder gets the scheme *source* but not the spec or key. Difficulty comes from parameters, keys and decoys, not obscurity. That's what makes the benchmark principled.
- **Key placement is the difficulty dial.** `metadata` (exiftool finds it) < `wordlist` (brute force after narrowing the technique) < `secret` (detection-only, partial credit).
- **Decoys** are planted in a tEXt chunk or after IEND. The scripted baseline falls for them every time; the LLM has to reason.
- **Two finders on the ladder** (`scripted-v0` vs `claude-finder-v1`), so the Elo chart shows a real gap.

## Timeline (solo), hard stop on features at 3:15

| Time | Do | Done when |
|---|---|---|
| **0:00–0:20** | Fill `.env`: `ANTHROPIC_API_KEY`, then ask the Archil team for an API key + disk + plan tier (concurrency). Ask: (1) is Free egress OK for apt/gem/pip in `setup_template.sh`, (2) does a fork of a **stopped** sandbox come up `running` or need `start()`, (3) typical fork latency. | Keys in `.env` |
| **0:20–0:50** | **B: Archil template.** `STEGOARENA_PROVIDER=archil uv run stegoarena template`. It creates the sandbox, uploads tools + setup script, installs the toolchain, stops it, caches its id in `.arena_cache/archil_template.json`, then forks once and prints tool availability. Fix whatever `setup_template.sh` trips on (stegseek is best-effort). | Fork prints `ok zsteg … arena_schemes ok` |
| 0:50–1:05 | **B: smoke on Archil.** `uv run stegoarena smoke --provider archil`. Then create the shared disk (`archil.create_disk(name="stegoarena")`), set `ARCHIL_DISK_ID`, rerun, and confirm objects under `arena/rounds/0/...` via `disk.list_objects`. | `cracked: true`, `fork_s` logged, trace on the disk |
| **1:05–1:45** | **C: live LLM finder.** `uv run stegoarena smoke --finder llm` (local first, then archil). Read the trace in the UI. Tune `SYSTEM` in `finder/agent.py` if it wastes turns (most likely fix: tell it to run `triage --budget 20` first). Check that thinking summaries appear as `reasoning` events. | Claude cracks known-easy in under 30s and cracks a `metadata`-keyed scheme |
| 1:45–2:15 | **C: Keenable on stage.** Confirm one finder trace shows `search_web`, then the command it informed (e.g. DCT histogram after the QIM snippet). If the model never searches, add a "if triage finds nothing, search once" rule. Confirm the hider writes `rounds/N/hider_research.json`. | One trace with search, then an informed command |
| **2:15–2:50** | **D: record the demo rounds.** `uv run stegoarena run --rounds 3 --schemes 4 --k 3 --finder both --hider llm --provider archil`. That's 24 attempts/round, about 5–8 min/round at concurrency 4. If the ladder is too easy or too hard, edit `hider/ladder.py`'s `LADDER` or the hider SYSTEM prompt and rerun. Copy `arena_data/` to `arena_data_demo/` as a frozen backup. | 3 rounds; Elo chart separates the two finders; at least one survivor and one decoy catch |
| 2:50–3:15 | **D: UI polish** for the story: lineage arrows (parent_id already shown), Archil fork time per attempt (in trace), a "crack moment" highlight. Rehearse the replay. | **Feature freeze 3:15** |
| 3:15–4:00 | Rehearse twice. One live round with cap: `--rounds 1 --schemes 2 --k 3`, `FINDER_BUDGET_S=60`. Fallback is the replay only. Write the 3-slide story. | |

## Demo script (≈3 min)
1. One line on the problem: static stego benchmarks saturate, so this one co-evolves.
2. UI, round 1: the scripted finder submits a **decoy** and scores 0. Claude rejects it and extracts the real flag (replay: `exiftool` finds `Comment`, triage with `--keys`, submit).
3. Round 3: the hider, after reading what got cracked (and its Keenable research), switches to DCT + wordlist key. Replay shows Claude narrowing the technique, searching, brute-forcing the wordlist… or surviving with partial credit.
4. Archil: one template, K forks per scheme, fork time in every trace, every command on the shared disk. `disk.list_objects("arena/rounds/3/attempts/")` live.
5. Live round, capped. If it flakes, go back to the replay.

## Risks and the switch to flip
| Risk | Mitigation (already wired) |
|---|---|
| Archil setup or egress fails | `STEGOARENA_PROVIDER=local` (works now). Docker if installed. Same interface |
| Archil disk write errors | `MirrorStore` mirror is best-effort; local store always written |
| Keenable down | `FallbackSearch` falls back to the stub per call; `KEENABLE_ENABLED=0` to force |
| No / rate-limited Anthropic key | finder→scripted, hider→ladder automatically; lower `--k` |
| Finder never cracks | known-easy smoke test; partial credit; the ladder starts at a trivial rung |
| Hider emits bad specs | structured outputs, `validate_spec`, round-trip check, ladder-mutation fill |
| Concurrency cap (Free = 5) | `MAX_CONCURRENCY=4` semaphore around fork→destroy |

## Known gaps / stretch (only before 3:15 if ahead)
- Lineage forking on Archil (the generator runs host-side today). Stretch: run `build_scheme` inside a "generator" sandbox and `fork()` it per child scheme so the lineage tree maps to real sandbox forks. Alternatively use `disk.create_branch` for per-round FS snapshots.
- Network lockdown of finder forks (`update_network(deny)`) needs Developer tier.
- The DCT scheme is QIM on block-DCT of a lossless PNG plane, not true JPEG-coefficient embedding (jpegio is fragile on macOS arm64). It's honest to call it "DCT-domain".
- `ArchilDiskStore.list` parses the `list_objects` result defensively; verify the field name on first use.
