"""retrieval.cache — one sqlite file for embeddings and rerank verdicts.

Owner: Berat Furkan Kocak (HW5, T14.6j).

**Why a cache at all.** Not primarily cost — a full re-index is ~$0.002. The
load-bearing reason is **reproducibility**: `text-embedding-3-small` is not
bit-deterministic (~1.2e-4 drift per component, asserted in
`tests/test_retrieval_online.py`), and two chunks closer together than that
margin can swap rank between runs. nDCG and MRR read position. So the Part 2
tables reproduce because the vectors come from here, not because the endpoint is
stable. Reranking has the same property for the more obvious reason.

**Why one sqlite file and not a directory of JSON.** The first implementation
wrote one JSON file per vector: 952 files, 31 MB, and a directory git would have
to be told to ignore. Three problems, all avoidable:

  - **Size.** JSON stores `0.023841859` as eleven characters. `array('f')` packs
    the same float in four bytes. 1536 dims goes from ~32 KB to 6 KB — the whole
    cache drops from 31 MB to ~6 MB.
  - **Inode churn.** A thousand small files is slow to stat, slow to delete, and
    noisy in every tool that walks the tree.
  - **It is one artefact.** The cache is a single derived thing with a single
    lifetime; representing it as a thousand files invited exactly the question
    of whether some of them should be committed. One file, gitignored, cannot.

float32 rather than float64 is deliberate and free: the precision lost (~1e-7)
is three orders of magnitude below the endpoint's own run-to-run drift, so it
cannot affect a ranking that was not already unstable.

Nothing here is durable. `store/cache/` is derived and may be deleted at any
time; the next index rebuilds it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from array import array
from pathlib import Path
from typing import Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    key   TEXT PRIMARY KEY,      -- sha256(model + text)
    model TEXT NOT NULL,
    dims  INTEGER NOT NULL,
    vec   BLOB NOT NULL          -- array('f') bytes, little-endian
);

CREATE TABLE IF NOT EXISTS reranks (
    key      TEXT PRIMARY KEY,   -- sha256(model + query + candidate ids)
    verdicts TEXT NOT NULL       -- json {position: band}
);
"""


def cache_dir() -> Path:
    """`RADF_RETRIEVAL_CACHE` is read at CALL time so tests can redirect it —
    the same rule every other store in this project follows."""
    override = os.environ.get("RADF_RETRIEVAL_CACHE")
    return Path(override) if override else _REPO_ROOT / "store" / "cache"


def cache_path() -> Path:
    return cache_dir() / "retrieval_cache.db"


def connect() -> sqlite3.Connection:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def embed_key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\x1f{text}".encode("utf-8")).hexdigest()


def rerank_key(model: str, query: str, ids: Sequence[str]) -> str:
    raw = "\x1f".join([model, query, *ids])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --- embeddings ---------------------------------------------------------------

def get_embeddings(keys: Sequence[str]) -> dict[str, list[float]]:
    """Cached vectors for `keys`. Missing keys are simply absent.

    A corrupt or short row is treated as a miss rather than an error — a bad
    cache entry must never be able to poison an index, and re-fetching one
    vector costs a fraction of a cent.
    """
    if not keys:
        return {}
    out: dict[str, list[float]] = {}
    conn = connect()
    try:
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            rows = conn.execute(
                f"SELECT key, dims, vec FROM embeddings"
                f" WHERE key IN ({','.join('?' * len(batch))})",
                batch,
            ).fetchall()
            for key, dims, blob in rows:
                buf = array("f")
                try:
                    buf.frombytes(blob)
                except ValueError:
                    continue
                if len(buf) == dims:
                    out[key] = buf.tolist()
    finally:
        conn.close()
    return out


def put_embeddings(model: str, items: Sequence[tuple[str, Sequence[float]]]) -> None:
    """Store `(key, vector)` pairs."""
    if not items:
        return
    rows = [
        (key, model, len(vector), array("f", vector).tobytes())
        for key, vector in items
    ]
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO embeddings (key, model, dims, vec) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET model=excluded.model,"
            " dims=excluded.dims, vec=excluded.vec",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def count_embeddings(keys: Sequence[str]) -> int:
    """How many of `keys` are already cached — for the indexer's report."""
    return len(get_embeddings(keys))


# --- rerank verdicts ----------------------------------------------------------

def get_rerank(key: str) -> Optional[dict[int, str]]:
    conn = connect()
    try:
        row = conn.execute("SELECT verdicts FROM reranks WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        return {int(k): v for k, v in json.loads(row[0]).items()}
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None


def put_rerank(key: str, verdicts: dict[int, str]) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO reranks (key, verdicts) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET verdicts = excluded.verdicts",
            (key, json.dumps({str(k): v for k, v in verdicts.items()})),
        )
        conn.commit()
    finally:
        conn.close()
