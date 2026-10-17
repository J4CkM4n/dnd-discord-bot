"""Ingest the SRD markdown into the rules SQLite index (schema v2).

v2 changes:
- Skips junk sources that only pollute retrieval: Spell_Lists*.md are bare
  name indexes ("Spells (S)" chunks), and 07_Spells/Spells_A-Z duplicates
  every spell already present one-per-file in 07_Spells/Spells_Each.
- Stores kind (spell/monster/item/feat/class/race/rule) inferred from the
  chapter directory, and src (the source file) for dedup and provenance.
- Heading paths: chunks carry a "chapter > section" breadcrumb so retrieval
  can show where a rule came from.

Chunks by markdown headings (fallback: filename), embeds with the local
embedding server, stores float32 vector BLOBs.

Usage: python tools/ingest_srd.py <srd_dir> <out_db> <embed_url> [model]
       python tools/ingest_srd.py <srd_dir> <out_db> --no-embeddings
"""

import os
import argparse
import re
import sqlite3
import sys
import time

import httpx
import numpy as np

MAX_CHARS = 1200
BATCH = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    path  TEXT NOT NULL DEFAULT '',
    kind  TEXT NOT NULL DEFAULT '',
    src   TEXT NOT NULL DEFAULT '',
    text  TEXT NOT NULL,
    vec   BLOB NOT NULL
);
"""

EXCLUDE_FILES = {"README.md", "Legal.md"}
EXCLUDE_FILE_PREFIXES = ("Spell_Lists",)
EXCLUDE_DIRS = {"Spells_A-Z"}  # dup of 07_Spells/Spells_Each

_KIND_BY_DIR = [
    ("01_Races", "race"), ("02_Classes", "class"), ("03_Characterization", "rule"),
    ("04_Equipment", "item"), ("05_Feats", "feat"), ("06_Gameplay", "rule"),
    ("07_Spells", "spell"), ("08_Gamemastering", "rule"),
    ("09_Magic_Items", "item"), ("10_Monsters", "monster"),
]


def _kind_for(rel: str) -> str:
    top = rel.replace("\\", "/").split("/")[0]
    for prefix, kind in _KIND_BY_DIR:
        if top.startswith(prefix):
            return kind
    return "rule"


def split_chunks(md: str, fallback_title: str) -> list[tuple[str, str, str]]:
    """Split markdown into (leaf title, breadcrumb path, chunk) triples,
    keyed by headings; oversized sections split on paragraph breaks."""
    lines = md.splitlines()
    sections: list[tuple[str, str, list[str]]] = []
    h1 = fallback_title
    cur_title, cur = fallback_title, []
    for line in lines:
        h = re.match(r"^(#{1,3})\s+(.*)", line)
        if h:
            if cur:
                sections.append((cur_title, h1, cur))
            level, text = len(h.group(1)), h.group(2).strip() or fallback_title
            if level == 1:
                h1 = text
            cur_title = text
            cur = []
        else:
            cur.append(line)
    if cur:
        sections.append((cur_title, h1, cur))

    out: list[tuple[str, str, str]] = []
    for title, section_h1, lines_ in sections:
        text = "\n".join(lines_).strip()
        if len(text) < 60:
            continue
        text = re.sub(r"\n{3,}", "\n\n", text)
        path = section_h1 if section_h1 != title else ""
        pieces = []
        while len(text) > MAX_CHARS:  # split oversized sections on paragraphs
            cut = text.rfind("\n\n", 400, MAX_CHARS)
            cut = cut if cut > 400 else MAX_CHARS
            pieces.append(text[:cut].strip())
            text = text[cut:].strip()
        pieces.append(text)
        out.extend((title, path, p) for p in pieces if len(p) >= 60)
    return out


def collect_md_files(srd_dir: str) -> list[tuple[str, str, str]]:
    """Return [(relative src path, label, content)] for every kept file."""
    docs = []
    for root, dirs, files in os.walk(srd_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in sorted(files):
            if not fn.lower().endswith(".md"):
                continue
            if fn in EXCLUDE_FILES or fn.startswith(EXCLUDE_FILE_PREFIXES):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, srd_dir)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError:
                continue
            label = os.path.splitext(fn)[0].replace("-", " ").replace("_", " ")
            docs.append((rel, label, content))
    return docs


def embed_batch(client: httpx.Client, url: str, texts: list[str],
                model: str = "") -> np.ndarray:
    body = {"input": texts}
    if model:
        body["model"] = model
    resp = client.post(f"{url}/v1/embeddings", json=body)
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    return np.asarray([d["embedding"] for d in data], dtype=np.float32)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("srd_dir")
    parser.add_argument("out_db")
    parser.add_argument("embed_url", nargs="?")
    parser.add_argument("model", nargs="?", default="")
    parser.add_argument("--no-embeddings", action="store_true", help="Build the /rules title/text index offline")
    args = parser.parse_args()
    srd_dir, out_db, embed_url, model = args.srd_dir, args.out_db, args.embed_url, args.model
    if not args.no_embeddings and not embed_url:
        parser.error("provide embed_url or --no-embeddings")
    docs = collect_md_files(srd_dir)
    if not docs:
        parser.error("no SRD Markdown files found; existing database was left unchanged")
    print(f"loaded {len(docs)} markdown files")

    if os.path.exists(out_db):
        os.remove(out_db)
    os.makedirs(os.path.dirname(os.path.abspath(out_db)), exist_ok=True)
    db = sqlite3.connect(out_db)
    db.executescript(SCHEMA)

    chunks: list[tuple[str, str, str, str, str]] = []
    for rel, label, content in docs:
        kind = _kind_for(rel)
        for title, path, text in split_chunks(content, label):
            chunks.append((title, path, kind, rel, text))
    print(f"chunks to embed: {len(chunks)}")

    t0 = time.time()
    with httpx.Client(timeout=120.0) as client:
        for i in range(0, len(chunks), BATCH):
            batch = chunks[i : i + BATCH]
            # embed title + body: many entries (spells, monsters, items) carry
            # their name only in the heading — body-only vectors miss "Fireball"
            vecs = ([np.array([], dtype=np.float32) for _ in batch] if args.no_embeddings else
                    embed_batch(client, embed_url, [f"{c[0]}\n{c[4]}" for c in batch], model))
            db.executemany(
                "INSERT INTO chunks (title, path, kind, src, text, vec) "
                "VALUES (?,?,?,?,?,?)",
                [(*c, v.tobytes()) for c, v in zip(batch, vecs)],
            )
            done = min(i + BATCH, len(chunks))
            rate = done / max(time.time() - t0, 1)
            print(f"  {done}/{len(chunks)} ({rate:.1f} chunks/s)", flush=True)
    db.commit()
    db.close()
    print(f"done in {time.time()-t0:.0f}s -> {out_db}")


if __name__ == "__main__":
    main()
