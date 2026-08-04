"""retrieval.search — the seam.

Owner: Berat Furkan Kocak (HW5, T14.6g).

    embed query -> dense top-30  ─┐
                                  ├─ RRF (k=60) ─> [rerank to top-k] ─> Hit[]
    BM25 over the same corpus  ───┘

**`search()` returns the RETRIEVER's order** (decision #59). Hit rate, precision
and recall ignore position; MRR and nDCG read it. Repack the list before scoring
and exactly two of the five degrade while the other three look unchanged — a
discrepancy that reads like a retrieval regression and is an instrumentation bug.
So any lost-in-the-middle reordering happens in `pack_for_llm` below, which is a
separate function applied *after* this returns, and never inside the search path.

Both arms run over the **same visibility-filtered corpus** (#24). The BM25 index
is built per call from `store.all_chunks`, which carries the same `WHERE` clause
as the vector scan — a lexical arm reading rows the dense arm could not would
leak private decisions through the fused ranking.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .bm25 import BM25
from .embed import EmbeddingError, embed_query
from .fuse import RRF_K, rrf, to_rank_list
from .rerank import rerank as rerank_candidates
from .store import IndexUnavailable, all_chunks, connect, dense_search
from .types import Chunk, Hit

# Depth per arm before fusion. See retrieval/rerank.py for why 30.
CANDIDATES = 30


class IndexEmpty(RuntimeError):
    """The index exists but holds nothing visible.

    Distinct from `IndexUnavailable`: "run the indexer" and "start the database"
    are different instructions, and collapsing them sends people to the wrong
    fix (same reasoning as decision #18 for the graph file).
    """


def search(
    query: str,
    *,
    k: int = 5,
    rerank: bool = True,
    source: str = "all",
    candidates: int = CANDIDATES,
    rrf_k: int = RRF_K,
    weights: Optional[Sequence[float]] = None,
    conn=None,
) -> list[Hit]:
    """Hybrid search. Returns up to `k` hits in retriever order.

    `weights` feeds the ablation table only — `(1, 0)` is dense-only, `(0, 1)`
    BM25-only. It is not a tuning knob.

    `conn` lets a caller (the eval harness, sweeping k) reuse one connection
    instead of reconnecting per query.
    """
    if not query or not query.strip():
        return []

    if conn is not None:
        return _search_with(conn, query, k, rerank, source, candidates, rrf_k, weights)
    with connect() as owned:
        return _search_with(owned, query, k, rerank, source, candidates, rrf_k, weights)


def _search_with(conn, query, k, do_rerank, source, candidates, rrf_k, weights) -> list[Hit]:
    corpus = all_chunks(conn, source=source)
    if not corpus:
        # A FILTER that matched nothing is an empty result, not a broken index.
        # Only an index with no visible chunks at all earns the error branch —
        # "run the indexer" and "that kind has no rows" are different answers,
        # and `source="components"` before the summariser has run must not look
        # like an outage.
        if not all_chunks(conn, source="all"):
            raise IndexEmpty("no visible chunks — run `python -m retrieval.index`")
        return []

    by_id: dict[str, Chunk] = {c.chunk_id: c for c in corpus}

    # --- dense arm -----------------------------------------------------------
    dense_scores: dict[str, float] = {}
    dense_ranking: list[str] = []
    try:
        vector = embed_query(query)
        for chunk, similarity in dense_search(conn, vector, n=candidates, source=source):
            by_id.setdefault(chunk.chunk_id, chunk)
            dense_scores[chunk.chunk_id] = similarity
            dense_ranking.append(chunk.chunk_id)
    except EmbeddingError:
        # A dead embeddings endpoint degrades to lexical-only rather than
        # failing the whole search. Recorded on the Hit (dense_score stays
        # None) so a run that silently lost an arm is visible in the trace
        # instead of looking like a bad day for the retriever.
        dense_ranking = []

    # --- lexical arm ---------------------------------------------------------
    bm25 = BM25([(c.chunk_id, c.text) for c in corpus])
    bm25_top = bm25.top(query, candidates)
    bm25_scores = dict(bm25_top)
    bm25_ranking = [chunk_id for chunk_id, _ in bm25_top]

    if not dense_ranking and not bm25_ranking:
        return []

    # --- fuse ----------------------------------------------------------------
    fused = rrf([dense_ranking, bm25_ranking], k=rrf_k, weights=weights)
    fused_ids = to_rank_list(fused)
    fused_scores = dict(fused)

    # --- rerank --------------------------------------------------------------
    rerank_scores: dict[str, float] = {}
    if do_rerank and fused_ids:
        pool = [by_id[cid] for cid in fused_ids[:candidates] if cid in by_id]
        try:
            reranked = rerank_candidates(query, pool, top_k=k)
            if reranked:
                rerank_scores = dict(reranked)
                fused_ids = [cid for cid, _ in reranked]
        except Exception:  # noqa: BLE001
            # Reranking is an improvement, not a dependency. If the model call
            # fails we return the fused ranking rather than nothing — and
            # `reranked=False` on the tool result says so honestly.
            rerank_scores = {}

    final = fused_ids[:k]
    return [
        Hit(
            chunk=by_id[cid],
            rank=position,
            dense_score=dense_scores.get(cid),
            bm25_score=bm25_scores.get(cid),
            rrf_score=fused_scores.get(cid),
            rerank_score=rerank_scores.get(cid),
        )
        for position, cid in enumerate(final, start=1)
        if cid in by_id
    ]


def pack_for_llm(hits: Sequence[Hit]) -> list[Hit]:
    """Reorder for the context window — strongest first and last.

    **Never call this before scoring.** It exists as a separate function, and is
    documented here rather than folded into `search()`, precisely because
    decision #59 is about keeping this reordering out of the measured path. It
    is the caller's choice, made after the metrics have seen the real ranking.
    """
    ordered = list(hits)
    if len(ordered) < 3:
        return ordered
    front, back, middle = [], [], []
    for i, hit in enumerate(ordered):
        (front if i % 2 == 0 else back).append(hit)
    middle = front + list(reversed(back))
    return middle


__all__ = ["search", "pack_for_llm", "IndexEmpty", "IndexUnavailable", "CANDIDATES"]
