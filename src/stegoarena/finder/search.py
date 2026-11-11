"""Web research behind a tiny interface: Keenable (real) or a stub (offline)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class SearchProvider(ABC):
    name = "base"

    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> list[dict]: ...

    @abstractmethod
    async def fetch(self, url: str, max_chars: int = 6000) -> dict: ...


class KeenableSearch(SearchProvider):
    """keenable SDK (keyless works at 1k req/h; KEENABLE_API_KEY lifts the cap)."""

    name = "keenable"

    def __init__(self, api_key: str | None = None):
        from keenable import AsyncKeenable

        self.client = AsyncKeenable(api_key=api_key, client_source="StegoArena")

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        res = await self.client.search(query, max_results=max_results, snippet_max_length=600)
        return [{"title": r.title, "url": r.url, "snippet": (r.snippet or "")[:600]} for r in res.results]

    async def fetch(self, url: str, max_chars: int = 6000) -> dict:
        page = await self.client.fetch(url)
        return {"url": page.url, "title": page.title, "content": (page.content or "")[:max_chars]}


class StubSearch(SearchProvider):
    """Offline fallback with canned steganalysis notes, so the tool never hard-fails."""

    name = "stub"
    NOTES = [
        {"title": "LSB steganalysis: chi-square / pairs-of-values attack", "url": "stub://lsb",
         "snippet": "Sequential LSB embedding equalises histogram pairs (2k, 2k+1). zsteg -a enumerates bit planes, channels, orders."},
        {"title": "Detecting QIM in block-DCT coefficients", "url": "stub://dct",
         "snippet": "Plot histogram of one mid-frequency 8x8 DCT coefficient: QIM embedding clusters values on multiples of delta/2."},
        {"title": "Palette (EzStego) steganography", "url": "stub://palette",
         "snippet": "Sort palette by luminance; message is in the index LSBs. Check for near-duplicate adjacent palette colours."},
        {"title": "Alpha channel hiding", "url": "stub://alpha",
         "snippet": "Inspect alpha for 254/255 values, and RGB of fully transparent pixels (viewers don't show them)."},
        {"title": "Password-protected payloads", "url": "stub://keys",
         "snippet": "Check PNG tEXt chunks (exiftool) for passphrases; otherwise try a wordlist against a narrowed scheme."},
    ]

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        q = query.lower()
        hits = [n for n in self.NOTES if any(w in (n["title"] + n["snippet"]).lower() for w in q.split())]
        return (hits or self.NOTES)[:max_results]

    async def fetch(self, url: str, max_chars: int = 6000) -> dict:
        for n in self.NOTES:
            if n["url"] == url:
                return {"url": url, "title": n["title"], "content": n["snippet"]}
        return {"url": url, "title": "", "content": "(offline stub: page unavailable)"}


class FallbackSearch(SearchProvider):
    """Try Keenable; on any error fall back to the stub for that call."""

    def __init__(self, primary: SearchProvider, fallback: SearchProvider):
        self.primary, self.fallback = primary, fallback
        self.name = primary.name

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        try:
            return await self.primary.search(query, max_results)
        except Exception as e:
            log.warning("keenable search failed (%s); stub used", e)
            return await self.fallback.search(query, max_results)

    async def fetch(self, url: str, max_chars: int = 6000) -> dict:
        try:
            return await self.primary.fetch(url, max_chars)
        except Exception as e:
            log.warning("keenable fetch failed (%s); stub used", e)
            return await self.fallback.fetch(url, max_chars)


def make_search(cfg) -> SearchProvider:
    if not cfg.keenable_enabled:
        return StubSearch()
    try:
        return FallbackSearch(KeenableSearch(cfg.keenable_api_key), StubSearch())
    except Exception as e:
        log.warning("keenable unavailable (%s); using stub", e)
        return StubSearch()
