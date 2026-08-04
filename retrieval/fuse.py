"""retrieval.fuse — reciprocal rank fusion.

Owner: Berat Furkan Kocak (HW5, T14.6b).

RRF combines rankings by **position**, never by score:

    score(d) = sum over rankings r of  1 / (RRF_K + rank_r(d))

That is the whole reason it is the right fuser here. The dense arm returns
cosine distances in [0, 2] and BM25 returns unbounded sums of IDF weights; any
weighted-sum fusion has to normalize two scales that mean different things, and
the normalization constant then quietly becomes the most important tuning
parameter in the system. Rank position is comparable across arms by
construction.

RRF_K = 60 is Cormack, Clarke & Buettcher's default and is kept, but the sweep
is reported rather than the constant asserted (see the README table). What the
constant controls is how sharply rank 1 outweighs rank 10:

    K=10  ->  1/11 vs 1/20   (2.0x)   aggressive, trusts each arm's top hit
    K=60  ->  1/61 vs 1/70   (1.15x)  flat, rewards agreement between arms
    K=200 ->  near-flat, effectively "how many arms found this at all"

Low K makes fusion behave like "whichever arm is most confident wins", which
defeats the point of running two. High K throws away the ranking information we
paid for. 60 keeps a document that BOTH arms rank moderately above one that a
single arm ranks first, which is the behaviour the failure modes in T14.8 need:
a chunk found by BM25 on an exact identifier *and* by vectors on meaning is
almost always the right chunk.
"""

from __future__ import annotations

from typing import Iterable, Sequence

RRF_K = 60


def rrf(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse ranked id lists into one, descending by fused score.

    `rankings` is a sequence of ranked `doc_id` lists, each best-first. A
    document absent from a ranking simply contributes nothing from that arm —
    it is **not** treated as ranked last, which would let the size of one arm's
    candidate pool change the other arm's contribution.

    `weights` defaults to equal weighting. It exists for the ablation table
    (dense-only is `weights=(1, 0)`), not as a tuning knob to reach for.
    """
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights must be the same length as rankings")

    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        if not weight:
            continue
        for position, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + position)

    # Tie-break on doc_id so a fused ranking is deterministic. Two chunks with
    # identical RRF scores are common (both arms rank them symmetrically), and
    # a nondeterministic order there would make nDCG unreproducible between
    # runs for reasons that have nothing to do with retrieval quality.
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def to_rank_list(scored: Iterable[tuple[str, float]]) -> list[str]:
    """`[(id, score), ...]` (already sorted) -> `[id, ...]`."""
    return [doc_id for doc_id, _ in scored]
