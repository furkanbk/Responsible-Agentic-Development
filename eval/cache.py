"""eval.cache — one sqlite file for generated answers and judged verdicts.

Owner: Dias Sarkytbaev (HW5, T14.14).

**Why a cache here is not an optimisation.** Part 2's five metrics are free and
reproduce exactly; Part 3's cost an LLM call each and reproduce only if something
makes them. Two different things would otherwise move between runs of the same
harness: the CHEAP answer under test (sampled text, different every call) and the
STRONG judge's verdict on it. Re-running `python -m eval.run_gen_eval` twice would
produce two different tables and no way to tell a real change from sampling noise.
So every model reply this phase makes is keyed and stored, and a re-run replays
them: the README numbers are reproducible for the same reason Berat's embedding
cache makes Part 2's reproducible (`retrieval/cache.py`), not because either
endpoint is deterministic.

**Why one table and not two.** An answer and a verdict are the same thing here —
one model reply to one fully-determined prompt. The `kind` that distinguishes them
is already inside the key, so a second table would only duplicate the schema.

**Where it lives.** Next to the retrieval cache, under `RADF_RETRIEVAL_CACHE` —
reusing `retrieval.cache.cache_dir()` rather than introducing a second env var.
The reason is test isolation, not tidiness: `tests/conftest.py::isolate_stores`
already redirects that variable for every test, so a scorer test cannot write into
the developer's real cache and no autouse fixture had to be touched to get that.
Its own **file** (`eval_cache.db`) because the schema is this phase's, and the
embeddings/reranks file belongs to `retrieval/` (CLAUDE.md §1).

Nothing here is durable. `store/cache/` is derived; deleting it costs another
judged run, which is exactly the bill this file exists to avoid paying twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence

from retrieval.cache import cache_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    key   TEXT PRIMARY KEY,      -- sha256(kind + model + prompt version + inputs)
    kind  TEXT NOT NULL,         -- 'answer' | 'faithfulness' | ...  (audit only)
    model TEXT NOT NULL,
    text  TEXT NOT NULL,
    usage TEXT NOT NULL          -- json; kept so a replayed run still reports cost
);
"""


def cache_path() -> Path:
    return cache_dir() / "eval_cache.db"


def connect() -> sqlite3.Connection:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def call_key(kind: str, model: str, version: str, parts: Sequence[str]) -> str:
    """A key over everything that could change the reply.

    `version` is the prompt version of the calling scorer. Editing a prompt and
    keeping the key would replay verdicts produced by the *old* prompt — the one
    cache bug that silently invalidates a whole results table while every number
    still looks plausible.
    """
    raw = "\x1f".join([kind, model, version, *parts])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_call(key: str) -> Optional[dict[str, Any]]:
    """`{"text": ..., "usage": {...}}` for a stored reply, or None.

    A corrupt `usage` blob degrades to `{}` rather than raising: a bad cost
    estimate must never be able to fail a run whose text is perfectly good.
    """
    conn = connect()
    try:
        row = conn.execute("SELECT text, usage FROM calls WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        usage = json.loads(row[1])
    except (json.JSONDecodeError, TypeError):
        usage = {}
    return {"text": row[0], "usage": usage if isinstance(usage, dict) else {}}


def put_call(key: str, kind: str, model: str, text: str, usage: Optional[dict]) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO calls (key, kind, model, text, usage) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, model=excluded.model,"
            " text=excluded.text, usage=excluded.usage",
            (key, kind, model, text, json.dumps(usage or {})),
        )
        conn.commit()
    finally:
        conn.close()


__all__ = ["cache_path", "connect", "call_key", "get_call", "put_call"]
