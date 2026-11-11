"""LLM hider: proposes scheme specs within the fixed technique family, learning from
what got cracked and how. Structured outputs (Pydantic) + our own validation; invalid
specs are retried, then replaced by ladder mutations so a round never stalls."""

from __future__ import annotations

import json
import logging
import random
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from ..arena_schemes import SchemeSpec, SpecError, difficulty, validate_spec
from ..finder.search import SearchProvider
from .ladder import ladder_specs, mutate

log = logging.getLogger(__name__)


class ProposedScheme(BaseModel):
    parent_scheme_id: str | None = Field(description="scheme_id this mutates, or null for a fresh design")
    rationale: str = Field(description="one or two sentences: why this should beat the finder")
    technique: Literal["lsb", "dct", "palette", "alpha"]
    channels: list[Literal["R", "G", "B"]]
    bit_plane: int = Field(description="0..2 (lsb only)")
    order: Literal["sequential", "prng"]
    dct_coeff: Literal["1,2", "2,1", "2,2", "3,1", "1,3", "3,2", "2,3"]
    dct_delta: int = Field(description="12..32 (dct only)")
    palette_colors: int = Field(description="16..256 (palette only)")
    alpha_mode: Literal["alpha_lsb", "transparent_rgb"]
    cipher: Literal["none", "xor", "aes"]
    key_source: Literal["none", "metadata", "wordlist", "secret"]
    decoy: Literal["none", "metadata_text", "trailer"]


class HiderProposal(BaseModel):
    schemes: list[ProposedScheme]


SYSTEM = """You are the HIDER in StegoArena, an adversarial image-steganography benchmark. You design schemes; an AI finder with stego CLI tools, the full source of the scheme library (Kerckhoffs's principle), a PNG-metadata reader, a ~160-word wordlist, and 90 seconds tries to recover the flag blind.

You may only choose parameters inside this fixed family:
- technique: lsb (channels, bit_plane 0..2) | dct (QIM on one 8x8 block-DCT coefficient of channels[0]; dct_coeff, dct_delta 12..32) | palette (index-LSB, palette_colors 16..256) | alpha (alpha_lsb | transparent_rgb)
- order: sequential | prng (permutation seeded by the key)
- cipher: none | xor | aes (payload encrypted with the key)
- key_source: none | metadata (key in a PNG text chunk - visible) | wordlist (key is a word from the finder's wordlist - brute-forceable) | secret (unrecoverable: finder can at best detect)
- decoy: none | metadata_text | trailer (plants a fake flag; finder is penalised for submitting it)
prng or a cipher requires key_source != none.

The benchmark is only interesting if schemes are crackable-but-hard. 'secret' keys make a scheme uncrackable, which wins the round but teaches nothing: use them for at most one scheme per round. Prefer subtle combinations the finder's past behaviour suggests it misses. Diversify techniques."""


class ClaudeHider:
    def __init__(self, cfg, search: SearchProvider | None = None):
        self.cfg = cfg
        self.search = search
        self.client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)

    async def research(self, feedback: list[dict]) -> list[dict]:
        """Optional Keenable step: look up how the finder cracked last round's schemes."""
        if not self.search or not feedback:
            return []
        cracked = sorted({f["spec"]["technique"] for f in feedback if f.get("cracked")})
        notes = []
        for tech in cracked[:2]:
            q = f"{tech} steganography detection resistant embedding variant"
            try:
                hits = await self.search.search(q, max_results=3)
                notes.append({"query": q, "provider": self.search.name, "results": hits})
            except Exception as e:
                log.warning("hider research failed: %s", e)
        return notes

    async def propose(self, round_no: int, n: int, feedback: list[dict], research: list[dict]) -> list[tuple[SchemeSpec, str | None, str]]:
        prompt = {
            "round": round_no,
            "schemes_wanted": n,
            "last_round_results": [
                {"scheme_id": f["scheme_id"], "spec": f["spec"], "cracked": f["cracked"], "best_score": f["best_score"],
                 "finder_signals": f.get("signals", [])[:3], "how": f.get("how")} for f in feedback],
            "research_notes": [{"query": r["query"], "titles": [h["title"] for h in r["results"]],
                                "snippets": [h["snippet"][:300] for h in r["results"]]} for r in research],
        }
        out: list[tuple[SchemeSpec, str | None, str]] = []
        for attempt in range(2):
            try:
                resp = await self.client.beta.messages.parse(
                    model=self.cfg.hider_model, max_tokens=16000, system=SYSTEM,
                    messages=[{"role": "user", "content": f"Propose exactly {n} schemes.\n\n{json.dumps(prompt, indent=1)}"}],
                    output_format=HiderProposal,
                    thinking={"type": "adaptive"}, output_config={"effort": "high"},
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                )
                if resp.stop_reason == "refusal" or resp.parsed_output is None:
                    raise ValueError(f"no parsed output (stop_reason={resp.stop_reason})")
                for p in resp.parsed_output.schemes:
                    d = p.model_dump()
                    try:
                        spec = validate_spec({k: d[k] for k in SchemeSpec.__dataclass_fields__})
                    except SpecError as e:
                        log.warning("hider spec rejected: %s", e)
                        continue
                    out.append((spec, p.parent_scheme_id, p.rationale))
                if len(out) >= n:
                    break
            except Exception as e:
                log.warning("hider attempt %d failed: %s", attempt + 1, e)
        rng = random.Random(round_no)
        fill = ladder_specs(round_no, n, [], [], seed=round_no)
        while len(out) < n:  # never stall: fill with harder ladder mutations
            spec, _, _ = fill[len(out) % len(fill)]
            out.append((mutate(spec, rng), None, "fallback: ladder mutation (LLM spec invalid)"))
        out.sort(key=lambda t: difficulty(t[0]))
        return out[:n]
