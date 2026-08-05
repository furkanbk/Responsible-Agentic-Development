"""eval.retrieval_metrics — the five rank-aware retrieval metrics (HW5, T14.11).

Owner: Alejandro Ramírez Trueba (Phase 14C, Part 2).

Plain Python, no dependency. **No evaluation library ships this family** — DeepEval
and Ragas both lack all five, and their `ContextualPrecisionMetric` /
`ContextualRecallMetric` are *judged* metrics measuring something else, so they are
not a substitute (assignment Part 2; decision #61). sklearn has `ndcg_score` but it
wants a dense score matrix where all we have is a ranked id list and a golden set,
so plain Python is both smaller and clearer here.

Every function takes **`ranked_ids` in the retriever's own order** and a `golden`
set of chunk ids. Three of the five (hit rate, precision, recall) ignore position;
two (MRR, nDCG) read it — which is exactly why `search()` returns the retriever's
order and any lost-in-the-middle repack happens downstream of it (decision #59).
Feed a repacked list to these and MRR and nDCG silently degrade while the other
three look unchanged. `ranked_ids_from_hits` below sorts by `Hit.rank` explicitly
so the harness can never pass the wrong order by accident.

**Empty-golden (out-of-corpus) cases are undefined, not zero** (assignment Part 2).
A case the corpus genuinely cannot answer has no relevant chunk, so precision /
recall / MRR / nDCG have no meaning — scoring them 0 would drag the average down
for correct behaviour. These functions therefore return `None` on an empty golden,
and `mean()` skips `None`; the harness counts and reports them separately.

Denominator note (precision@k): "what fraction of *what you returned* was relevant"
(assignment), so the denominator is `min(k, len(ranked_ids))`, not a flat `k`. The
two agree whenever the retriever returned at least `k` hits (the usual case); they
differ only when it returned fewer, where dividing by `k` would penalise a short
list for being short rather than for being wrong.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

# The metric names, in the order the report tables present them. Kept here so the
# harness and the tests agree on one spelling and one order.
METRIC_NAMES: tuple[str, ...] = (
    "hit_rate",
    "precision",
    "recall",
    "mrr",
    "ndcg",
)


def ranked_ids_from_hits(hits: Sequence) -> list[str]:
    """Chunk ids in retriever order, read from `Hit.rank` — never list order.

    `search()` already returns hits in rank order, but sorting on the field the
    contract says carries position (decision #59, and `retrieval/types.py`) means
    a caller that reordered the list — a repack, a `set`, a dict round-trip —
    cannot quietly feed MRR and nDCG the wrong order. If two hits share a rank
    (they should not) the id breaks the tie, only so the result is deterministic.
    """
    return [h.chunk_id for h in sorted(hits, key=lambda h: (h.rank, h.chunk_id))]


def _top_k(ranked_ids: Sequence[str], k: int) -> list[str]:
    if k <= 0:
        return []
    return list(ranked_ids[:k])


def hit_rate_at_k(ranked_ids: Sequence[str], golden: Iterable[str], k: int) -> Optional[float]:
    """1.0 if any golden chunk made the top-k cut, else 0.0. `None` on empty golden.

    The coarsest signal: did retrieval put *anything* useful in front of the model.
    """
    gold = set(golden)
    if not gold:
        return None
    return 1.0 if gold.intersection(_top_k(ranked_ids, k)) else 0.0


def precision_at_k(ranked_ids: Sequence[str], golden: Iterable[str], k: int) -> Optional[float]:
    """Fraction of the returned top-k that was relevant. `None` on empty golden.

    Denominator is what was actually returned (`min(k, len)`), see module docstring.
    """
    gold = set(golden)
    if not gold:
        return None
    top = _top_k(ranked_ids, k)
    if not top:
        return 0.0
    hits = sum(1 for cid in top if cid in gold)
    return hits / len(top)


def recall_at_k(ranked_ids: Sequence[str], golden: Iterable[str], k: int) -> Optional[float]:
    """Fraction of the golden chunks that made the top-k cut. `None` on empty golden."""
    gold = set(golden)
    if not gold:
        return None
    top = set(_top_k(ranked_ids, k))
    return len(gold & top) / len(gold)


def rr_at_k(ranked_ids: Sequence[str], golden: Iterable[str], k: int) -> Optional[float]:
    """Reciprocal rank of the first golden chunk within the top-k. `None` on empty golden.

    The per-case term of MRR: 1 if a golden chunk is at rank 1, 1/2 at rank 2, and
    0 if none appears in the top-k. Reads position — see decision #59.
    """
    gold = set(golden)
    if not gold:
        return None
    for i, cid in enumerate(_top_k(ranked_ids, k), start=1):
        if cid in gold:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked_ids: Sequence[str], golden: Iterable[str], k: int) -> Optional[float]:
    """Binary-relevance nDCG@k. `None` on empty golden.

    The one metric that notices a relevant chunk sliding from position 1 to 5:
    DCG discounts each hit by `1/log2(rank+1)`, and the ideal ranking packs every
    golden chunk (up to k) at the top. nDCG = DCG / IDCG lands in [0, 1].
    """
    gold = set(golden)
    if not gold:
        return None
    top = _top_k(ranked_ids, k)
    dcg = sum(1.0 / math.log2(rank + 1) for rank, cid in enumerate(top, start=1) if cid in gold)
    ideal_hits = min(len(gold), max(k, 0))
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


# The five, keyed by the name the tables use. rr_at_k is the per-case term whose
# mean over cases is MRR, so it is registered under "mrr".
_METRIC_FNS = {
    "hit_rate": hit_rate_at_k,
    "precision": precision_at_k,
    "recall": recall_at_k,
    "mrr": rr_at_k,
    "ndcg": ndcg_at_k,
}


def metrics_for_case(ranked_ids: Sequence[str], golden: Iterable[str], k: int) -> dict[str, Optional[float]]:
    """All five metrics at one k for one case. `None` throughout on empty golden."""
    gold = set(golden)
    return {name: _METRIC_FNS[name](ranked_ids, gold, k) for name in METRIC_NAMES}


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    """Mean over the defined values, skipping `None` (the empty-golden cases).

    Returns `None` when every value is `None` — an average over nothing is
    undefined, and reporting 0.0 there would invent a data point.
    """
    defined = [v for v in values if v is not None]
    if not defined:
        return None
    return sum(defined) / len(defined)


def aggregate(per_case: Sequence[dict[str, Optional[float]]]) -> dict[str, Optional[float]]:
    """Macro-average each metric across cases, `None`-skipping (see `mean`)."""
    return {name: mean(case.get(name) for case in per_case) for name in METRIC_NAMES}


__all__ = [
    "METRIC_NAMES",
    "ranked_ids_from_hits",
    "hit_rate_at_k",
    "precision_at_k",
    "recall_at_k",
    "rr_at_k",
    "ndcg_at_k",
    "metrics_for_case",
    "mean",
    "aggregate",
]
