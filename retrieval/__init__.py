"""retrieval — hybrid search over the authored description of the codebase.

Owner: Berat Furkan Kocak for the contracts (HW5, Phase A);
Alejandro Ramírez Trueba for the layer itself (Phase B).

Closes the gap ARCHITECTURE.md §6 has carried since HW1: "retrieval over the
graph — currently exact lookup only". `query_component_graph` needs a dotted
module id and `retrieve_decisions` needs an exact `symbol_uid`, so a request
phrased the way people actually phrase them ("make the approval prompt clearer")
matches nothing.

Shape of the layer, dense and lexical fused rather than either alone:

    chunker   node_summaries + decisions + doc sections -> Chunk[]
    embed     text-embedding-3-small via OpenRouter, disk-cached by text sha
    store     Postgres + pgvector, exact scan (600 chunks does not want ANN)
    bm25      hand-written Okapi BM25 — Postgres FTS ranking has no IDF, and
              IDF is the entire reason the lexical arm wins exact-term queries
    fuse      reciprocal rank fusion, k=60
    rerank    LLM reranker on CHEAP, cached on (query, candidate ids)
    search    the seam: search(query, *, k, rerank, source) -> list[Hit]

Submodules are imported lazily below `types`, so importing `retrieval.types`
(all the eval harness needs) never drags in `psycopg`.
"""

from __future__ import annotations

from .types import (
    Anchor,
    Chunk,
    ChunkKind,
    EvalCase,
    Hit,
    KINDS,
)

__all__ = [
    "Anchor",
    "Chunk",
    "ChunkKind",
    "EvalCase",
    "Hit",
    "KINDS",
    "search",
    "pack_for_llm",
]


def __getattr__(name: str):
    """Lazy re-export of the search seam.

    `from retrieval import search` works, but `import retrieval` alone does not
    require Postgres to be installed — which keeps the offline scorer tests in
    Phase 14C independent of Phase 14B's infrastructure.
    """
    if name in ("search", "pack_for_llm"):
        from . import search as _search
        return getattr(_search, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
