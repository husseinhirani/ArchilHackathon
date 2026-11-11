"""Runtime config from env / .env. Every external key is optional: missing keys
select the local fallback (LocalStore, local sandbox provider, stub search)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v if v not in ("", None) else default


@dataclass
class Config:
    # LLM
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    finder_model: str = field(default_factory=lambda: _env("FINDER_MODEL", "claude-sonnet-5-5"))
    hider_model: str = field(default_factory=lambda: _env("HIDER_MODEL", "claude-opus-5-5"))
    finder_effort: str = field(default_factory=lambda: _env("FINDER_EFFORT", "medium"))
    finder_budget_s: float = field(default_factory=lambda: float(_env("FINDER_BUDGET_S", "90")))
    finder_max_turns: int = field(default_factory=lambda: int(_env("FINDER_MAX_TURNS", "16")))
    # Archil
    archil_api_key: str | None = field(default_factory=lambda: _env("ARCHIL_API_KEY"))
    archil_region: str = field(default_factory=lambda: _env("ARCHIL_REGION", "aws-us-east-1"))
    archil_disk_id: str | None = field(default_factory=lambda: _env("ARCHIL_DISK_ID"))
    archil_base_image: str = field(default_factory=lambda: _env("ARCHIL_BASE_IMAGE", "python:3.11-bookworm"))
    # Keenable
    keenable_api_key: str | None = field(default_factory=lambda: _env("KEENABLE_API_KEY"))
    keenable_enabled: bool = field(default_factory=lambda: _env("KEENABLE_ENABLED", "1") == "1")
    # Arena
    provider: str = field(default_factory=lambda: _env("STEGOARENA_PROVIDER", "local"))
    arena_root: Path = field(default_factory=lambda: Path(_env("ARENA_ROOT", str(REPO_ROOT / "arena_data"))))
    cache_dir: Path = field(default_factory=lambda: REPO_ROOT / ".arena_cache")
    max_concurrency: int = field(default_factory=lambda: int(_env("MAX_CONCURRENCY", "5")))


def get_config() -> Config:
    cfg = Config()
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg
