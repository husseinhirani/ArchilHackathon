# StegoArena

**AI agents that hide secrets in images, and AI agents that hunt for them, getting better through experience instead of bigger models.**

A *hider* agent designs a way to hide a secret flag inside a picture. *Finder* agents try to recover it blind, each inside its own forked [Archil](https://archil.com) sandbox loaded with real steganography tools. Both sides research on the web with [Keenable](https://keenable.ai), learn from every round, and evolve on an Elo ladder. Every agent runs on **the same Claude model at the same effort**. Any improvement comes from what the agents learn, not from a model upgrade.

Built for the Archil hackathon.

---

## Contents
- [The 60-second pitch](#the-60-second-pitch)
- [Demo script (5 minutes)](#demo-script-5-minutes)
- [Before the demo: checklist](#before-the-demo-checklist)
- [Setup](#setup)
- [The five pages](#the-five-pages)
- [How it works](#how-it-works)
- [What makes the agents dynamic](#what-makes-the-agents-dynamic-not-just-a-bigger-model)
- [How we use Archil](#how-we-use-archil)
- [How we use Keenable](#how-we-use-keenable)
- [CLI reference](#cli-reference)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## The 60-second pitch

1. **The hider hides.** It picks a technique (pixel bits, frequency domain, palette, transparency), scrambling, encryption, where the key lives, and whether to plant a fake "decoy" flag. Then it embeds a secret into an image. The change is invisible: typically under 0.1% of pixels, by at most a few brightness levels.
2. **The finders hunt.** Each attempt forks a fresh Archil sandbox (~2s) with `zsteg`, `binwalk`, `exiftool`, `steghide`, `stegseek` and Python. A finder has 90 seconds. It can run commands, search the web, save its own tools, and even **fork its live sandbox** to chase two hypotheses at once.
3. **Both evolve.** After each round the answers are revealed. Finders rewrite their own playbook and keep code they wrote; hiders study what got cracked and **read the champion finder's playbook** to design around it. The weakest agents are retired and replaced by coached children of the best.
4. **The proof.** On a held-out set of puzzles no agent trained on, we compare the bare model with evolved agents. Same model, same effort; only the mind changes.

---

## Demo script (5 minutes)

Start the site with `uv run stegoarena ui` and open **http://127.0.0.1:8000**.

| # | Where | What to do | What the judges see | Time |
|---|---|---|---|---|
| 1 | **Live** (`/live`) | Upload a judge's photo → leave **Fair fight** → **Start the match** | The hider searches Keenable ("DCT coefficient QIM steganalysis…"), reads the top finder's playbook from the shared Archil disk, and designs a scheme. The secret goes into *their* photo, with an original / with-secret / **difference ×64** view. Then 4 finders race side by side in parallel Archil sandboxes. | ~90s |
| 2 | **Live**, the result banner | Point out who won and who **fell for the decoy** | In our test run, both evolved finders cracked it in ~23s while the bare model submitted the decoy, **on the same model**. | 20s |
| 3 | **Lab** (`/`) → *The result* | Read the headline number | Held-out crack rate: bare model vs best agent, same model and effort. | 30s |
| 4 | **Lab** → *Watch a highlight* | Click **"Parallel hypotheses"** or **"A web search changed its next move"** | The replay jumps to the moment: a live fork of the sandbox and the winning branch, or ❓ unfamiliar signal → 🔎 Keenable search → the command it led to. | 45s |
| 5 | **Lab** → *The agents' minds* | Click the top finder | The playbook it wrote for itself, and a Python tool it coded and kept. | 30s |
| 6 | *(optional)* **Live** → **All out** | Run a second match | The hider goes as hard as the rules allow. In our test it read the finders' "narrow to bit-plane 0" rule and hid the secret in bit-plane 2; nobody cracked it. | 90s |
| 7 | *(optional)* **Hide & Seek** (`/hs`) | A judge hides a sentence and races the AI themselves | A human vs AI race with a live trace. | 60s |

**If something misbehaves live:** finished matches are listed under **Recent matches** on `/live`; open one to show the full run. The Lab replays recorded runs from disk and works offline.

---

## Before the demo: checklist

```bash
git pull
uv sync
uv run pytest -q                                   # 121 tests, no keys needed
cp .env.example .env                               # then fill in keys (see Configuration), if not done
uv run stegoarena template --provider archil       # builds/caches the tool-loaded sandbox (~30s first time)
uv run stegoarena smoke --provider archil          # an easy image must come back "cracked": true
uv run stegoarena ui                               # http://127.0.0.1:8000
```

- [ ] `.env` has `ANTHROPIC_API_KEY`, `ARCHIL_API_KEY`, `ARCHIL_REGION` (ours is **aws-us-west-2**), `ARCHIL_DISK_ID`, `STEGOARENA_PROVIDER=archil`.
- [ ] `/live` header shows `hider claude-opus-5-5 · finders claude-sonnet-5-5 · archil` (not "offline mode").
- [ ] Do one practice **Fair fight** with a phone photo, so there's a good finished match under *Recent matches*.
- [ ] The Lab shows data. If *The result* is empty, run the [learning-curve commands](#grow-and-measure-the-agents) beforehand; they take ~25–30 min and cost API credit.
- [ ] Hard-refresh the browser (Cmd+Shift+R) after pulling, so the newest pages load.
- [ ] At most **2 live matches** run at once; a third shows a friendly "give it a minute".

---

## Setup

**Requirements:** Python 3.11+, [uv](https://docs.astral.sh/uv/), and optionally Docker. Archil, Anthropic and Keenable keys are optional: without them everything falls back to local equivalents (see below).

```bash
git clone https://github.com/husseinhirani/ArchilHackathon.git
cd ArchilHackathon
uv sync
cp .env.example .env      # add your keys
uv run stegoarena ui      # http://127.0.0.1:8000
```

**What runs without keys:**

| Missing | Falls back to |
|---|---|
| `ANTHROPIC_API_KEY` | a scripted finder (fixed triage script) and a curated ladder hider |
| `ARCHIL_API_KEY` | `STEGOARENA_PROVIDER=local`: subprocess "sandboxes" on your machine (dev only, no isolation) |
| `ARCHIL_DISK_ID` | the local `arena_data/` folder only (no shared-disk mirror) |
| `KEENABLE_API_KEY` | keyless Keenable (1,000 requests/hour); `KEENABLE_ENABLED=0` uses an offline stub |

---

## The five pages

| Page | URL | What it's for |
|---|---|---|
| **Live** | `/live` | Upload a photo and watch the hider design and embed, then finders race live. **The main demo.** |
| **Lab** | `/` | The research dashboard: how it works, the headline result, one-click highlights, rounds with plain-English scheme cards, step-by-step replays, each agent's playbook and tools, and the Elo ladder. Every technical term has an ⓘ tooltip. |
| **Hide & Seek** | `/hs` | Hide your own secret (easy / medium / hard), share a link, and a friend races the AI. Player tools: bit-plane viewer with a noise meter, file inspector, decoder. |
| **Learn** | `/learn` | Guided lessons (easy → medium → hard) on how steganography works, with an AI tutor: concept → hide → signals → practice → graduate. |
| **How to use** | `/guide` | The in-app manual: reading the Lab and replays, glossary, a 3-minute judge path, commands, and an FAQ. |

---

## How it works

```
          ┌──────────── Keenable: research detection methods ────────────┐
          ▼                                                              │
   HIDER agent (Opus) ── designs a scheme ──► generator: embed + round-trip check
   reads the champion's playbook                    │  image.png / spec.json / truth.json
                                                    ▼
                      ┌───────────── shared Archil disk (every trace mirrored) ─────────────┐
                      │                                                                    │
   Archil template sandbox (all stego tools) ──fork ~2s──► FINDER agent (Sonnet), 90s       │
                                              ──fork──► FINDER agent  ── live fork ──► branch b0 / b1
                                              ──fork──► ...           ── Keenable search on unfamiliar signals
                                                    │
                                                    ▼
                         scorer: partial credit + Elo ──► reflection: playbook + validated tools
                                                    │
                                                    ▼
                         selection: retire the weakest genome, coach a child of the best
```

- **Kerckhoffs's principle.** Finders get the full source of the hiding library inside their sandbox, but not the settings or the key. Difficulty comes from parameters, keys and decoys, not secrecy of the algorithm.
- **Techniques:** LSB (any channel set, bit planes 0–2), DCT-domain QIM (8×8 blocks), palette index LSB, alpha channel / transparent pixels. Options: PRNG ordering seeded by the key; XOR or AES encryption; keys in metadata, a 157-word wordlist, or a ~16k "biglist"; decoys in metadata, after the file end, or as a second hidden payload.
- **Solvability gate.** Unrecoverable keys are banned. Every fielded scheme is crackable in principle, so hiders win by being subtle, not impossible.
- **Scoring:** detection 15% + technique 20% + parameters 20% + flag 45%; submitting a decoy costs 15%. Elo is tracked for finders, schemes and hiders.

---

## What makes the agents dynamic (not just a bigger model)

The model and effort are **locked**: `evolve` refuses to continue if they change. What differs between agents is their **genome**, stored on the shared Archil disk:

| Mechanism | What happens |
|---|---|
| **Personas** | Finders start as *Statistician* (measure first), *Locksmith* (key hunting) or *Scout* (survey and research). Hiders start as *Decoy Artist*, *Frequency Purist* or *Key Smith*. |
| **Learned playbook** | After each round the answers are revealed. Each finder reflects on its own traces and rewrites a ≤30-rule playbook that's injected into its prompt next time. |
| **Self-written toolkit** | Mid-attempt, finders can `save_tool` a Python script. Kept tools are **tested inside an Archil fork** (model-written code never runs on the host) and shipped into every future fork. |
| **Forked hypotheses** | `branch` live-forks the running sandbox, memory and all (~9s), plus the conversation, into 2–3 parallel hypotheses. The first branch to extract the flag wins. |
| **Learned curiosity** | When tool output shows a signal the agent's playbook doesn't cover, the harness nudges a Keenable search. Once the playbook learns the signal, the nudge stops. |
| **Opponent modelling** | Hiders read the champion finder's playbook from the shared disk and design schemes its rules will miss. |
| **Evolution** | Each generation the lowest-Elo finder and hider are retired, and a "coach" call writes a child that inherits the champion's playbook and tools with a mutated strategy. |
| **Held-out evaluation** | A frozen puzzle set that no agent learns from measures real improvement: `static` (bare model) vs champions over generations. |

---

## How we use Archil

- **One template, many forks.** `stegoarena template` builds a sandbox with every stego tool (~30s, cached). Each finder attempt is a fork of it: ~2s alone, ~5s with 8 in parallel.
- **Live forks for branching.** A finder can fork its *running* sandbox mid-attempt; the child keeps the parent's memory, processes and files.
- **Shared file system.** Every scheme, trace, command log, result, genome, playbook and tool is mirrored to an Archil disk (`arena/…`), so any crack can be replayed and agents can read each other's notebooks.
- **Safe execution of agent-written code.** New tools are validated inside a fork before they are kept.
- **Clean-up.** Forks are destroyed after each attempt; after a full run only the stopped template remains.

## How we use Keenable

- **Hiders:** before each generation (and each Live match), evidence-driven research. They search the most-cracked techniques, the detection method the opponent relies on, and variants of what survived. Each note records *why* it was searched.
- **Finders:** search when they meet an unfamiliar signal. In the replay you see ❓ *signal* → 🔎 *search "…", because it spotted …* → the next command marked *"informed by Keenable"*.

---

## CLI reference

All commands: `uv run stegoarena <command> [--provider local|docker|archil]`.

| Command | What it does |
|---|---|
| `ui [--port 8000]` | Start the website (all five pages). |
| `template` | Build/cache the Archil (or Docker) template sandbox and print tool availability. |
| `smoke [--finder scripted\|llm]` | A known-easy scheme must be cracked; a failure means a tooling problem. |
| `run --rounds 1 --schemes 4 --k 3 [--finder llm\|scripted\|both] [--hider llm\|ladder]` | Classic rounds: K identical forks per scheme. |
| `evolve --generations 3 [--schemes-per-hider 2]` | Population mode: research, design, race, reflect, select. |
| `eval-set [--n 8]` | Build the frozen held-out puzzle set. |
| `eval --genome static\|champion\|<id> [--repeats 2]` | One point on the same-model learning curve. |
| `hide -m "secret" [--level easy\|medium\|hard] [--photo p.jpg] [--hint …]` | Create a Hide & Seek challenge link from the terminal. |
| `hs-seed [--per-level 3] [--finder llm\|scripted]` | AI-only Hide & Seek races, to calibrate crack rate per level. |

### Grow and measure the agents
```bash
uv run stegoarena eval-set --n 6
uv run stegoarena eval --genome static --repeats 2       # the bare model
uv run stegoarena evolve --generations 3                 # ~18 attempts per generation
uv run stegoarena eval --genome champion --repeats 2     # same model, evolved mind
```
This takes roughly 25–30 minutes on Archil and spends API credit (≈100 finder attempts plus hider, reflection and coach calls).

---

## Configuration

`.env` (copy from `.env.example`; never commit it, it's git-ignored):

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Enables the Claude hider and finders. |
| `FINDER_MODEL` / `HIDER_MODEL` | `claude-sonnet-5-5` / `claude-opus-5-5` | Locked for a whole `evolve` run. |
| `FINDER_EFFORT`, `FINDER_BUDGET_S`, `FINDER_MAX_TURNS` | `medium`, `90`, `16` | Per-attempt limits. |
| `ARCHIL_API_KEY`, `ARCHIL_REGION`, `ARCHIL_DISK_ID` | none, `aws-us-west-2`, none | The key only works in its own region. |
| `STEGOARENA_PROVIDER` | `local` | `archil` for the demo. |
| `MAX_CONCURRENCY` | `4` | 8 parallel forks verified on our account. |
| `KEENABLE_API_KEY`, `KEENABLE_ENABLED` | none, `1` | Keyless works; `0` uses the offline stub. |
| `ARENA_ROOT` | `./arena_data` | Where the UI reads from. Point it at a copy to freeze demo data. |
| `RANDOM_COVERS` | `1` | `0` makes covers reproducible from seeds. |

---

## Project layout

```
src/stegoarena/
  arena_schemes/   hiding techniques, ciphers, decoys, triage + fast key search (copied into every sandbox)
  generator/       cover images and build_scheme (embed + round-trip check)
  sandbox/         SandboxProvider: archil | docker | local, plus setup_template.sh
  finder/          Claude finder loop (agent.py), scripted baseline, Keenable search, traces, learning
  hider/           Claude hider (structured outputs + research), curated ladder + mutation
  genome.py        finder/hider genomes (persona, playbook, toolkit)
  population.py    evolve: research → design → race → reflect → select
  eval.py          frozen held-out evaluation
  live.py          Live mode pipeline (upload → hider → embed → finders race)
  hideseek.py      Hide & Seek game logic       academy.py + lessons.json   Learn mode
  scorer.py        partial credit + Elo         store.py   local store + Archil disk mirror
  ui/              FastAPI app, routes, highlights; static/ pages (index, live, hs, learn, guide) + arena.css
tests/             121 tests (no keys or network needed)
```

**Data** (`arena_data/`, mirrored to the Archil disk under `arena/`): `rounds/{n}/schemes|attempts/…`, `genomes/…`, `population.json`, `elo.json`, `eval/…`, `live/…`, `challenges/…` (Hide & Seek), `lessons/…`. Live matches use rounds 7000+ and never touch Elo or the Lab.

---

## Testing

```bash
uv run pytest -q
```
The suite runs offline with the local provider and fake Claude clients. It covers:
- every scheme round-trips, and the key search recovers wordlist and biglist keys;
- the scripted finder cracks the known-easy scheme (smoke);
- branching forks the sandbox and the conversation, and the first extraction wins;
- tools are committed only if they run;
- Keenable nudges, searches and the commands they inform are linked;
- Hide & Seek validation and player-facing errors;
- a full Live match over HTTP;
- UI highlights, and that no page swallows keystrokes.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Archil `401` | Wrong region: the key only works in its own region (ours: `ARCHIL_REGION=aws-us-west-2`). |
| `name must be 1-63 lowercase letters…` | Fixed: sandbox names are sanitised. Pull the latest. |
| `/live` says "offline mode" | `ANTHROPIC_API_KEY` is missing from `.env`, or the server was started before you added it. Restart `stegoarena ui`. |
| A text box won't accept typing | Fixed (a keydown handler returned `false`). Pull, then hard-refresh. |
| "Two live matches are already running" | Wait for one to finish (~1–2 min). |
| The Lab's *The result* or highlights are empty | Run the [learning-curve commands](#grow-and-measure-the-agents). Highlights such as branching and Keenable appear once runs have produced them. |
| Leftover sandboxes on Archil | Shouldn't happen. Check with `archil` (`client.sandboxes.list()`); only the stopped template should remain. |

**Security:** API keys live only in `.env` (git-ignored). Rotate any key that has been pasted into chats or screenshots.
