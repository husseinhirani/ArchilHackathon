"""Cover images: real photos from ./covers if present, else procedural natural-ish images."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

COVERS_DIR = Path(__file__).resolve().parents[3] / "covers"


def make_cover(seed: int, size: int = 512) -> Image.Image:
    """Smooth gradients + soft shapes + sensor-like noise. Deterministic per seed."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size] / size
    base = np.stack(
        [
            rng.uniform(40, 200) + rng.uniform(-60, 60) * x + rng.uniform(-60, 60) * y,
            rng.uniform(40, 200) + rng.uniform(-60, 60) * np.sin(3 * x + rng.uniform(0, 3)),
            rng.uniform(40, 200) + rng.uniform(-60, 60) * np.cos(2 * y + rng.uniform(0, 3)),
        ],
        axis=-1,
    )
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(img)
    for _ in range(rng.integers(6, 14)):
        x0, y0 = rng.integers(0, size, 2)
        r = int(rng.integers(size // 20, size // 4))
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            draw.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=color)
        else:
            draw.rectangle([x0 - r, y0 - r // 2, x0 + r, y0 + r // 2], fill=color)
    img = img.filter(ImageFilter.GaussianBlur(radius=size / 128))
    arr = np.array(img, dtype=np.float64) + rng.normal(0, 4.0, (size, size, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def pick_cover(seed: int, size: int = 512) -> tuple[Image.Image, str]:
    photos = sorted(p for p in COVERS_DIR.glob("*") if p.suffix.lower() in {".png", ".jpg", ".jpeg"}) if COVERS_DIR.exists() else []
    if photos:
        p = photos[seed % len(photos)]
        img = Image.open(p).convert("RGB")
        img.thumbnail((size, size))
        return img, p.name
    return make_cover(seed, size), f"procedural:{seed}"
