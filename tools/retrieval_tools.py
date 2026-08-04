"""tools.retrieval_tools — semantic search over the corpus, as a tool.

Owner: Berat Furkan Kocak for the stub contract (HW5, Phase A);
Alejandro Ramírez Trueba fills the body (Phase B). The signature, docstring and
return shape below are the contract the rest of HW5 is built against — do not
change them without agreement (CLAUDE.md §1).

**It is a tool, not a pipeline stage.** Nothing runs retrieval before the model
speaks. The model decides whether this question needs the corpus at all, and can
re-query with a refined phrasing when the first result set is thin — which
already works, because `guards.detect_stall` stops an *identical* repeated call
but lets a genuinely different query through (#10).

Read-only and reversible -> ungated (CLAUDE.md §5).

Scope stays ambient (#25): there is deliberately no `user_id` parameter here.
Decision chunks carry a visibility and the SQL query ANDs the `visible_to`
predicate against `session.current_user()` — filtered in the query, never by
handing the model rows plus an instruction to ignore some (#24).
"""

from __future__ import annotations

from typing import Literal

from retrieval.search import IndexEmpty, IndexUnavailable, search

MAX_K = 20


def search_corpus(
    query: str,
    k: int = 5,
    rerank: bool = True,
    source: Literal["all", "components", "decisions", "docs"] = "all",
) -> dict:
    """Search the project corpus in plain language and get back ranked passages.

    Use this when the question is conceptual or the target is unknown: "which
    component handles the approval prompt", "why is the graph a JSON file",
    "where would I change how runs are logged". It matches meaning as well as
    wording, so it works when you do not know the exact module name.

    Constrained params:
      query   required; a natural-language question or a description of the
              change you are making. Full phrasing beats keywords here.
      k       how many passages to return, 1..20. Default 5.
      rerank  re-score the fused candidates for relevance before returning.
              Default true; costs one extra model call and roughly 200ms.
      source  enum: "components" (what each module/symbol is for),
              "decisions" (why it is that way), "docs" (project documentation),
              or "all".

    When NOT to call: do not call this for an exact structural lookup you can
    already name — "what imports agentlib.core" is `query_component_graph`, and
    "which decisions constrain Module:agentlib.loop" is `retrieve_decisions`.
    Those are exact joins and this is a ranked guess; using search where a join
    will do gets you approximately the right answer instead of the right one.
    Do not call it to read a file either — that is `read_source_file`.

    Returns (contract): ranked passages in RETRIEVER order, e.g.
        {"query": <str>, "k": <int>, "reranked": <bool>, "source": <str>,
         "count": <int>,
         "results": [{"chunk_id": <str>, "kind": "component"|"decision"|"doc",
                      "symbol_uid": <str>|None, "symbol": <str>|None,
                      "heading_path": <str>, "source_path": <str>,
                      "rank": <int>, "score": <float>, "text": <str>}, ...]}
    On failure, a structured error the loop branches on rather than a plausible
    empty result (Part B, B2):
        {"error": "index_unavailable"|"index_empty"|"invalid_args"}

    `results` is in the order the retriever produced. Any lost-in-the-middle
    repacking happens downstream of this call, never inside it — MRR and nDCG
    read this order, and two of the five metrics degrade silently if they are
    fed a repacked list instead.
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "invalid_args",
                "details": ["query must be a non-empty string"]}
    if not isinstance(k, int) or isinstance(k, bool) or not (1 <= k <= MAX_K):
        return {"error": "invalid_args",
                "details": [f"k must be an int in [1, {MAX_K}], got {k!r}"]}
    if source not in ("all", "components", "decisions", "docs"):
        return {"error": "invalid_args",
                "details": [f"source must be one of all|components|decisions|docs,"
                            f" got {source!r}"]}

    try:
        hits = search(query, k=k, rerank=bool(rerank), source=source)
    except IndexUnavailable:
        # Postgres is down / psycopg missing. Its own branch, never an empty
        # result: "the index is unreachable" and "nothing matched" call for
        # different fixes and must not look alike (Part B, B2).
        return {"error": "index_unavailable"}
    except IndexEmpty:
        return {"error": "index_empty"}

    return {
        "query": query,
        "k": k,
        # What actually happened, not what was asked for: reranking degrades to
        # fused order when the model call fails, and reporting `True` there
        # would make the trace lie about which ranking produced this.
        "reranked": bool(rerank) and any(h.rerank_score is not None for h in hits),
        "source": source,
        "count": len(hits),
        "results": [
            {
                "chunk_id": h.chunk.chunk_id,
                "kind": h.chunk.kind,
                "symbol_uid": h.chunk.symbol_uid,
                "symbol": h.chunk.symbol,
                "heading_path": h.chunk.heading_path,
                "source_path": h.chunk.source_path,
                "rank": h.rank,
                "score": round(h.rerank_score if h.rerank_score is not None
                               else (h.rrf_score or 0.0), 6),
                "text": h.chunk.text,
            }
            for h in hits
        ],
    }
