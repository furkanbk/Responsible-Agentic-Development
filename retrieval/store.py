"""retrieval.store — the pgvector index.

Owner: Berat Furkan Kocak (HW5, T14.6e).

**Entirely derived.** Everything here is rebuildable from `store/radf.db` plus
the repo's markdown, which is why it is allowed to live outside `store/` in a
container that can be thrown away. The authored overlay is not migrating here
(decision #62) — that separation is the project's core idea and a running
Postgres does not change it.

**Exact scan, no HNSW/IVFFlat.** At ~700 chunks an approximate index returns the
same neighbours as a sequential scan while adding a build step, a tuning
parameter nobody can justify, and a recall cliff that only appears at sizes we
do not have. "We chose HNSW" would be a copy-pasted default; "the corpus does
not warrant approximation" is a decision. Revisit at ~100k chunks.

**Visibility is a WHERE clause** (#24). The predicate is applied in the same
query as the vector scan — never as a post-filter over rows already fetched,
and never as an instruction to the model. `current_user()` is read from the
ambient contextvar; there is no `user_id` parameter anywhere in this module's
public surface for a caller to get wrong (#25).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterable, Optional, Sequence

from agentlib.session import current_user

from .embed import EMBED_DIM
from .types import TEAM, Chunk

DEFAULT_DSN = "postgresql://radf:radf@localhost:5433/radf"

TABLE = "chunks"

_SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS {TABLE} (
    chunk_id     TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,          -- component | decision | doc
    text         TEXT NOT NULL,
    embedding    vector({EMBED_DIM}),
    symbol_uid   TEXT,
    symbol       TEXT,
    heading_path TEXT NOT NULL DEFAULT '',
    source_path  TEXT NOT NULL DEFAULT '',
    content_sha  TEXT NOT NULL DEFAULT '',
    visibility   TEXT NOT NULL DEFAULT 'team'
);
CREATE INDEX IF NOT EXISTS idx_chunks_kind ON {TABLE}(kind);
CREATE INDEX IF NOT EXISTS idx_chunks_uid  ON {TABLE}(symbol_uid);
CREATE INDEX IF NOT EXISTS idx_chunks_vis  ON {TABLE}(visibility);
"""

_COLUMNS = (
    "chunk_id", "kind", "text", "symbol_uid", "symbol",
    "heading_path", "source_path", "content_sha", "visibility",
)


class IndexUnavailable(RuntimeError):
    """Postgres is unreachable, or psycopg is not installed.

    Its own exception so `search_corpus` returns the `index_unavailable` error
    branch instead of an empty result set — "the index is down" and "nothing
    matched" are different answers and must not look alike (Part B, B2).
    """


def dsn() -> str:
    """Read at CALL time so tests can redirect it, same rule as every store."""
    return os.environ.get("RADF_PG_DSN") or DEFAULT_DSN


@contextmanager
def connect(target: Optional[str] = None):
    """A connection to the index, or `IndexUnavailable`."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment problem
        raise IndexUnavailable(
            "psycopg is not installed — pip install -r requirements.txt"
        ) from exc
    try:
        conn = psycopg.connect(target or dsn(), connect_timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise IndexUnavailable(
            f"cannot reach Postgres at {target or dsn()}: {exc}."
            " Try `docker compose up -d`."
        ) from exc
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema(conn) -> None:
    """Idempotent. Schema creation lives here rather than in `docker-compose.yml`
    for the same reason `overlay/db.py::init_db` owns the sqlite DDL: one place
    that runs on every connect cannot drift from the code that reads it."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
    conn.commit()


def _to_vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{v:.7g}" for v in vector) + "]"


def upsert_chunks(conn, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> int:
    """Write chunks + vectors. Returns rows written."""
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings must be the same length")
    if not chunks:
        return 0
    rows = [
        (
            c.chunk_id, c.kind, c.text, c.symbol_uid, c.symbol,
            c.heading_path, c.source_path, c.content_sha, c.visibility,
            _to_vector_literal(v),
        )
        for c, v in zip(chunks, embeddings)
    ]
    cols = ", ".join(_COLUMNS)
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {TABLE} ({cols}, embedding)"
            f" VALUES ({', '.join(['%s'] * len(_COLUMNS))}, %s::vector)"
            " ON CONFLICT (chunk_id) DO UPDATE SET"
            " kind = EXCLUDED.kind, text = EXCLUDED.text,"
            " symbol_uid = EXCLUDED.symbol_uid, symbol = EXCLUDED.symbol,"
            " heading_path = EXCLUDED.heading_path, source_path = EXCLUDED.source_path,"
            " content_sha = EXCLUDED.content_sha, visibility = EXCLUDED.visibility,"
            " embedding = EXCLUDED.embedding",
            rows,
        )
    conn.commit()
    return len(rows)


def replace_all(conn, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> int:
    """Full reindex: the index is derived, so it is replaced wholesale.

    Same rule as `scan_repository_structure` on the derived graph (#16) — a
    merge would leave chunks for sections that no longer exist, and a retrieval
    hit on deleted documentation is worse than a miss.
    """
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {TABLE}")
    conn.commit()
    return upsert_chunks(conn, chunks, embeddings)


def _visibility_clause(user_id: Optional[str]) -> tuple[str, list]:
    """The trust boundary, as SQL. Mirrors `overlay.db.visible_to` (#24)."""
    if not user_id:
        return "visibility = %s", [TEAM]
    return "visibility IN (%s, %s)", [TEAM, f"user:{user_id}"]


def _kind_clause(source: str) -> tuple[str, list]:
    mapping = {"components": "component", "decisions": "decision", "docs": "doc"}
    if source in ("all", "", None):
        return "", []
    kind = mapping.get(source, source)
    return "kind = %s", [kind]


def _row_to_chunk(row) -> Chunk:
    return Chunk(
        chunk_id=row[0], kind=row[1], text=row[2], symbol_uid=row[3], symbol=row[4],
        heading_path=row[5], source_path=row[6], content_sha=row[7], visibility=row[8],
    )


def _where(source: str, user_id: Optional[str]) -> tuple[str, list]:
    clauses, params = [], []
    vis_sql, vis_params = _visibility_clause(user_id)
    clauses.append(vis_sql)
    params += vis_params
    kind_sql, kind_params = _kind_clause(source)
    if kind_sql:
        clauses.append(kind_sql)
        params += kind_params
    return " AND ".join(clauses), params


def dense_search(
    conn,
    query_vector: Sequence[float],
    *,
    n: int = 30,
    source: str = "all",
) -> list[tuple[Chunk, float]]:
    """Top `n` by cosine distance, filtered in the same query (#24).

    Returns `(chunk, similarity)` with similarity in [0, 1] — pgvector's `<=>`
    is a *distance*, and handing a distance to a fuser that assumes higher-is-
    better is a bug that produces a perfectly plausible reversed ranking.
    """
    where, params = _where(source, current_user())
    cols = ", ".join(_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols}, embedding <=> %s::vector AS distance"
            f" FROM {TABLE} WHERE {where} AND embedding IS NOT NULL"
            " ORDER BY distance ASC LIMIT %s",
            [_to_vector_literal(query_vector), *params, n],
        )
        rows = cur.fetchall()
    return [(_row_to_chunk(r), 1.0 - float(r[-1])) for r in rows]


def all_chunks(conn, *, source: str = "all") -> list[Chunk]:
    """Every visible chunk — the BM25 arm's corpus.

    The same visibility predicate as `dense_search`, deliberately: a lexical arm
    that saw rows the dense arm could not would leak private decisions through
    the fused ranking, which is precisely the hole #24 exists to close.
    """
    where, params = _where(source, current_user())
    cols = ", ".join(_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {cols} FROM {TABLE} WHERE {where}", params)
        rows = cur.fetchall()
    return [_row_to_chunk(r) for r in rows]


def fetch_chunks(conn, chunk_ids: Iterable[str]) -> dict[str, Chunk]:
    """Chunks by id, still visibility-filtered."""
    ids = list(chunk_ids)
    if not ids:
        return {}
    where, params = _where("all", current_user())
    cols = ", ".join(_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols} FROM {TABLE} WHERE {where} AND chunk_id = ANY(%s)",
            [*params, ids],
        )
        rows = cur.fetchall()
    return {r[0]: _row_to_chunk(r) for r in rows}


def count(conn, *, source: str = "all") -> int:
    where, params = _where(source, current_user())
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE {where}", params)
        return int(cur.fetchone()[0])


def counts_by_kind(conn) -> dict[str, int]:
    where, params = _where("all", current_user())
    with conn.cursor() as cur:
        cur.execute(f"SELECT kind, COUNT(*) FROM {TABLE} WHERE {where} GROUP BY kind", params)
        return {row[0]: int(row[1]) for row in cur.fetchall()}


def drop_table(conn) -> None:
    """Remove the index entirely. Safe: it is derived and rebuildable."""
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.commit()
