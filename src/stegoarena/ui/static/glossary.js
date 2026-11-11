// One glossary for the Lab tooltips and the How-to-use page (keep them in sync by sharing this file).
window.GLOSSARY = {
  "Steganography": "Hiding a message inside an ordinary-looking file, here a PNG image, so nobody knows it is there.",
  "Hider": "The AI that designs how to hide a secret flag in an image (technique, channels, scrambling, encryption, where the key lives, decoys).",
  "Finder": "The AI that tries to recover the flag blind, using real stego tools inside a sandbox, with a 90-second budget.",
  "Archil sandbox": "An isolated cloud machine. We build one with every stego tool installed, then fork it (copy it in ~2s) for every attempt.",
  "Live fork": "Copying a sandbox while it is running, including its memory and files, so the finder can try two ideas in parallel from the same state (~9s).",
  "Keenable": "A web-search API for agents. Hiders research detection methods before designing; finders search when they hit a signal they don't recognise.",
  "Elo": "A chess-style rating. Beating a stronger opponent gains more points. Finders and schemes are rated against each other.",
  "Genome": "Everything that makes one agent different from another except the model: persona, playbook, toolkit and branching policy.",
  "Persona": "A short strategy description, e.g. the Statistician measures first and searches second.",
  "Playbook": "Rules the finder writes for itself after each round, once the answers are revealed. They are put in its prompt next time.",
  "Toolkit": "Python scripts the finder wrote and saved mid-attempt. They are tested in a sandbox and shipped into every future fork.",
  "Same model": "Every agent here uses the same Claude model and effort setting. Any improvement comes from experience, not a bigger model.",
  "Held-out eval": "A fixed set of puzzles no agent ever trains on. Used to measure real improvement between generations.",
  "Decoy": "A fake flag planted somewhere obvious (metadata, after the file end, other pixels). Submitting it is penalised.",
  "Partial credit": "Scores reward detecting the hiding, naming the technique and its settings, and finally recovering the flag.",
  "LSB": "Least significant bit: hiding data in the lowest bit of each pixel's colour, which changes it by at most 1 out of 255.",
  "Bit plane": "One bit of every pixel, viewed as a black-and-white image. Hidden data looks like noise in an otherwise smooth plane.",
  "DCT": "Hiding in the image's frequency domain (8x8 blocks, like JPEG) by nudging one coefficient. Invisible to pixel-level tools.",
  "PRNG order": "Instead of writing bits left-to-right, the pixels are visited in a scrambled order seeded by the key.",
  "Key source": "Where the key lives: in the file's metadata (easy), one of 157 wordlist words (medium), or one of ~16k word+number combos (hard).",
  "Solvability gate": "Hiders may not use unrecoverable keys. Every scheme must be crackable in principle, so hiders win by being subtle, not impossible.",
};
window.tip = (term, extra = "") => `<span class="tip" tabindex="0" data-tip="${(window.GLOSSARY[term] || extra).replace(/"/g, "&quot;")}">i</span>`;

// Plain-English scheme description (Lab cards + Live page).
const _CH = { R: "red", G: "green", B: "blue" };
const _PLANE = ["lowest", "second-lowest", "third-lowest"];
window.describeSpec = function (sp) {
  const ch = (sp.channels || []).map((c) => _CH[c] || c);
  const chs = ch.length === 3 ? "red, green and blue" : ch.join(" and ");
  let s = { lsb: `Hidden in the ${_PLANE[sp.bit_plane] || "low"} bit of the ${chs} pixels`,
    dct: `Hidden in the frequency domain of the ${_CH[sp.channels?.[0]] || ""} channel (8×8 DCT coefficient ${sp.dct_coeff}, step ${sp.dct_delta})`,
    palette: `Hidden in the palette indexes of a ${sp.palette_colors}-colour image`,
    alpha: sp.alpha_mode === "transparent_rgb" ? "Hidden in the colours of invisible, fully transparent pixels" : "Hidden in the lowest bit of the transparency (alpha) channel" }[sp.technique] || sp.technique;
  const extra = [sp.order === "prng" ? "scrambled in a key-seeded order" : "", sp.cipher && sp.cipher !== "none" ? `${sp.cipher.toUpperCase()}-encrypted` : ""].filter(Boolean);
  if (extra.length) s += ", " + extra.join(", ");
  s += ".";
  const key = { metadata: "Key: sitting in the file's metadata.", wordlist: "Key: one of 157 words in the shared wordlist.",
    biglist: "Key: one of ~16,000 word+number combos.", secret: "Key: secret." }[sp.key_source];
  if (key) s += " " + key;
  const decoy = { metadata_text: "A fake flag is planted in the metadata.", trailer: "A fake flag is stuck after the end of the file.",
    pixel: "A fake message is hidden in other pixels." }[sp.decoy];
  if (decoy) s += " " + decoy;
  return s;
}
window.levelOf = (d) => d < 3.5 ? ["easy", "Easy"] : d < 6.5 ? ["medium", "Medium"] : ["hard", "Hard"];
