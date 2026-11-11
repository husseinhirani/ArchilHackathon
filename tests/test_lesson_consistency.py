"""Lesson text must agree with the level specs it describes.

This is a keyword check, not a reading of the prose: it catches the kinds of drift that
crept into lessons.json (wrong channel count, wrong bit plane, wrong order, cipher or key
source), so a future edit that contradicts LEVELS fails the suite.
"""

import copy
import json
import re
from pathlib import Path

from stegoarena.arena_schemes import validate_spec
from stegoarena.hideseek import LEVELS

LESSON_FILE = Path(__file__).resolve().parents[1] / "src" / "stegoarena" / "lessons.json"
COUNT_WORD = {1: "one", 2: "two", 3: "three"}
CHANNEL_WORDS = {"R": "red", "G": "green", "B": "blue"}
UNUSED_CHANNEL_OK = re.compile(r"untouched|left alone|unchanged|still|stay|natural|clean|healthy|not used", re.I)


def lesson_strings(lesson: dict):
    yield from lesson["concept"]
    yield from lesson["in_the_wild"]
    for s in lesson["signals"]:
        yield from s.values()
    yield from lesson["hints"]["practice"]
    yield lesson["title"]


def lesson_problems(lessons: dict, levels: dict = LEVELS) -> list[str]:
    problems: list[str] = []
    for lv, raw in levels.items():
        spec = validate_spec(dict(raw)).to_dict()  # LEVELS lists only non-default fields
        text = list(lesson_strings(lessons[lv]))
        chans = spec["channels"]
        n = len(chans)
        has = lambda pat: any(re.search(pat, t, re.I) for t in text)  # noqa: E731

        # Capacity: "<count> bits per pixel" must match the number of channels used.
        for t in text:
            for m in re.finditer(r"\b(one|two|three)\s+bits?\s+per\s+pixel", t, re.I):
                if m.group(1).lower() != COUNT_WORD[n]:
                    problems.append(f"{lv}: says '{m.group(0)}' but the spec uses {n} channel(s)")
        # Channel claims are only checked where the text says how the payload is hidden (title and
        # concept). Signal cards and hints legitimately talk about readings on every channel.
        embedding = [lessons[lv]["title"], *lessons[lv]["concept"]]
        if n < 3 and any(re.search(r"all three|three (colour |color )?channels|every (colour |color )?channel|each (colour |color )?channel", t, re.I)
                         for t in embedding):
            problems.append(f"{lv}: describes all channels, but the spec uses only {chans}")

        # Channels: a channel the spec doesn't use may only be mentioned as staying unchanged.
        for c, word in CHANNEL_WORDS.items():
            if c in chans:
                continue
            for t in embedding:
                if re.search(rf"\b{word}\b", t, re.I) and not UNUSED_CHANNEL_OK.search(t):
                    problems.append(f"{lv}: mentions {word}, which the spec does not use: {t[:60]!r}")

        # Bit plane: 0 means the lowest plane, so no text should point at a higher one.
        if spec["bit_plane"] == 0 and has(r"middle bit|mid bit|one bit plane above|plane above the lowest|higher plane"):
            problems.append(f"{lv}: describes a higher bit plane, but the spec uses plane 0")

        # Order: sequential placement must not be described as scattered, and PRNG must be.
        if spec["order"] == "sequential" and has(r"pseudorandom|prng|scatter|shuffl"):
            problems.append(f"{lv}: describes scattered order, but the spec is sequential")
        if spec["order"] == "prng" and not has(r"pseudorandom|prng|scatter"):
            problems.append(f"{lv}: spec is PRNG-ordered, but no text describes a scattered order")

        # Cipher.
        cipher = spec["cipher"]
        if cipher == "none" and has(r"\bxor\b|\baes\b|\bcipher\b|encrypt"):
            problems.append(f"{lv}: mentions a cipher, but the spec has none")
        if cipher == "xor" and not has(r"\bxor\b"):
            problems.append(f"{lv}: spec uses XOR, but no text says XOR")
        if cipher != "xor" and has(r"\bxor\b"):
            problems.append(f"{lv}: mentions XOR, but the spec uses {cipher}")
        if cipher == "aes" and not has(r"\baes\b"):
            problems.append(f"{lv}: spec uses AES, but no text says AES")

        # Key source.
        if spec["key_source"] == "metadata" and not has(r"metadata|text chunk"):
            problems.append(f"{lv}: key is in metadata, but no text says so")
        if spec["key_source"] == "wordlist" and not has(r"wordlist|dictionary"):
            problems.append(f"{lv}: key is a wordlist word, but no text says so")
    return problems


def test_lessons_agree_with_level_specs():
    lessons = json.loads(LESSON_FILE.read_text())
    assert lesson_problems(lessons) == []


def test_checker_catches_known_mistakes():
    lessons = json.loads(LESSON_FILE.read_text())
    bad = copy.deepcopy(lessons)
    bad["easy"]["concept"][0] = "This level hides one bit per pixel in the lowest bit."
    bad["medium"]["concept"][0] = "Medium spreads the payload across all three colour channels."
    bad["medium"]["concept"][1] = "The bits are scattered with a pseudorandom sequence."
    bad["hard"]["concept"][0] = "The payload sits in a middle bit plane."
    problems = lesson_problems(bad)
    assert any(p.startswith("easy:") and "one bit per pixel" in p for p in problems)
    assert any(p.startswith("medium:") and "all channels" in p for p in problems)
    assert any(p.startswith("medium:") and "scattered order" in p for p in problems)
    assert any(p.startswith("hard:") and "higher bit plane" in p for p in problems)
