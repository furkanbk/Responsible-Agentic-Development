"""retrieval.rerank — LLM reranking over the fused candidates.

Owner: Berat Furkan Kocak (HW5, T14.6f).

**Depth: retrieve 30 per arm -> fuse -> rerank to top-k (default 5).**

30 is chosen from the failure it prevents. The reranker cannot recover a chunk
neither arm retrieved, so the candidate pool has to be deep enough to contain
the right answer even when both arms rank it mediocrely — and in the T14.8
before/after set, the lexical-vs-semantic mismatch cases put the correct chunk
between fused positions 8 and 20. A pool of 10 would have left nothing to
rerank; beyond ~40 the marginal chunk is noise the reranker has to spend
judgment rejecting.

**What the extra latency costs and buys — measured, not estimated.** One `CHEAP`
call over 30 candidates takes **~3.4-4.2s uncached** (median ~4.0s) against
**~60ms** for the un-reranked path. That is a ~60x multiplier and it is the
honest headline: this is not a cheap step. Cached it drops to ~60-140ms, which
is why the eval harness can re-run the full k-sweep for free but a live agent
turn cannot.

What it buys is the difference between "the right chunk is somewhere in the top
30" and "the right chunk is at position 1". On *"what stops the agent from
looping forever"* the fused ranking returns the README overview and
`agents.executor`; reranking replaces them with `agentlib.loop` and the relevant
ARCHITECTURE sections. That is precisely what the rank-weighted metrics (MRR,
nDCG) measure and what fusion alone does not deliver: RRF rewards agreement
between two arms that are both matching on surface features, so a near-duplicate
both arms like outranks the one chunk that answers the question.

Whether ~4s per search is worth it is a real trade, which is why `rerank` is a
parameter the caller sets rather than a decision baked into the pipeline — and
why Part 2 measures it both ways instead of asserting the win.

**Why an LLM and not a cross-encoder.** A cross-encoder is the textbook choice
and is deterministic. It is also `sentence-transformers` + torch — on a 7 GB
machine already running Postgres, a ~2.5 GB install (or ~300 MB from the CPU
index) for one ranking step. The determinism argument is the real one, and the
cache answers it: results are keyed on `(model, query, candidate ids)`, so a
repeated evaluation replays byte-identical scores. The rerank-on/off tables in
Part 2 are therefore reproducible *and* free to re-run, which is what those
metrics being "free" depends on.

Grading is on **named values, not a 0-10 score** — the same rule
`monitor/judge.py` follows (#37). A model asked for a number produces
suspiciously smooth distributions and clusters everything at 7; asked to sort
candidates into three named buckets it commits, and the buckets are auditable by
a human reading the trace.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from agentlib.core import CHEAP, call

from .cache import get_rerank, put_rerank, rerank_key
from .types import Chunk

# Named relevance bands, most useful first. The gaps are wide on purpose: the
# reranker's job is to separate "answers the question" from "mentions the same
# words", and a fine-grained scale invites the model to split hairs it cannot
# actually see.
BANDS: dict[str, float] = {
    "answers": 1.0,      # directly answers the query
    "related": 0.5,      # same subject, does not answer it
    "unrelated": 0.0,    # matched on surface features only
}

# How much of each candidate the reranker sees. Enough to judge relevance,
# short enough that 30 candidates fit one cheap call comfortably.
SNIPPET_CHARS = 420

_INSTRUCTION = """You rank search results for a codebase knowledge base.

For EACH numbered candidate, decide how it relates to the QUERY:
  answers   - it directly answers the query
  related   - same subject, but it does not answer the query
  unrelated - it only shares words or names with the query

Judge the candidate on its own content. A candidate is not "answers" merely
because it repeats the query's terms.

Reply with ONE line per candidate, nothing else:
<number>: <answers|related|unrelated>
"""


class RerankUnavailable(RuntimeError):
    """The reranker could not produce a usable ordering."""


def _parse(text: str, n: int) -> dict[int, str]:
    """Pull `<index>: <band>` pairs out of the reply.

    Tolerant by design: the model may wrap lines, add bullets, or number from
    zero. Anything unparseable is simply absent, and an absent verdict falls
    back to fused order rather than dropping the candidate — a reranker that
    silently deletes results is worse than one that declines to reorder them.
    """
    out: dict[int, str] = {}
    for match in re.finditer(r"(\d+)\s*[:.\)-]\s*(answers|related|unrelated)", text or "", re.I):
        index = int(match.group(1))
        band = match.group(2).lower()
        if 1 <= index <= n:
            out[index] = band
    return out


def rerank(
    query: str,
    candidates: Sequence[Chunk],
    *,
    top_k: int = 5,
    model: str = CHEAP,
    use_cache: bool = True,
) -> list[tuple[str, float]]:
    """Re-score `candidates` for `query`. Returns `[(chunk_id, score)]`, best first.

    `candidates` must arrive in fused order — it is the tie-break and the
    fallback, so a shuffled input silently changes the output.
    """
    if not candidates:
        return []

    ids = [c.chunk_id for c in candidates]
    key = rerank_key(model, query, ids)

    verdicts: Optional[dict[int, str]] = get_rerank(key) if use_cache else None

    if verdicts is None:
        listing = "\n\n".join(
            f"[{i}] {c.heading_path}\n{c.text[:SNIPPET_CHARS]}"
            for i, c in enumerate(candidates, start=1)
        )
        result = call(
            f"QUERY: {query}\n\nCANDIDATES:\n{listing}",
            system=_INSTRUCTION,
            model=model,
            max_output_tokens=16 * len(candidates) + 256,
        )
        verdicts = _parse(result.text or "", len(candidates))
        if use_cache:
            put_rerank(key, verdicts)

    # Fused position breaks ties WITHIN a band, so the reranker reorders across
    # bands and defers to fusion inside one. An unjudged candidate keeps its
    # fused position at the bottom band rather than disappearing.
    scored: list[tuple[str, float, int]] = []
    for position, chunk in enumerate(candidates, start=1):
        band = verdicts.get(position)
        score = BANDS.get(band, 0.0) if band else 0.0
        scored.append((chunk.chunk_id, score, position))

    scored.sort(key=lambda t: (-t[1], t[2]))
    return [(chunk_id, score) for chunk_id, score, _ in scored[:top_k]]
