"""retrieval.types — the chunk contract.

Owner: Berat Furkan Kocak (HW5, T14.2). **Frozen contract.**

This module exists so that the retrieval layer (Phase B) and the eval harness
(Phase C) can be built in parallel against the same shape without either owning
it. Changing a field name here breaks both; change it by agreement, not in
passing (CLAUDE.md §1).

Three things a chunk carries that are easy to leave out and expensive to add
back:

`symbol_uid` — every chunk joins the existing uid space (#22), so a retrieval
hit can be handed straight to `retrieve_decisions`, the impact walk, or
`apply_change` without a second lookup. A chunk that only knew its own text
would be a dead end.

`heading_path` — the full ancestry ("§5 Decision log > #21"), prefixed onto the
chunk text at index time. Without it, a chunk reading "JSON, because it is
diffable and any scan may replace it" is unretrievable by any query that does
not already use those words. This is what makes acronym and pronoun queries
resolvable at all.

`rank` — the position the RETRIEVER returned this at. MRR and nDCG read it, and
they must read it before any lost-in-the-middle repacking reorders the list.
Repacking is a separate step applied downstream of `search()` for exactly this
reason; if you ever find yourself setting `rank` after a repack, the metrics
have silently stopped measuring the retriever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# What a chunk was built from. Doubles as the `source` filter on `search_corpus`.
ChunkKind = Literal["component", "decision", "doc"]

KINDS: tuple[ChunkKind, ...] = ("component", "decision", "doc")

TEAM = "team"


@dataclass(frozen=True)
class Chunk:
    """One indexed unit of the corpus.

    Frozen: a chunk that mutated between retrieval and scoring would make the
    eval numbers unreproducible in a way that is very hard to see.
    """

    chunk_id: str
    kind: ChunkKind
    text: str
    symbol_uid: Optional[str] = None
    symbol: Optional[str] = None
    heading_path: str = ""
    source_path: str = ""
    content_sha: str = ""
    # 'team' | 'user:<id>'. Only decision chunks are ever scoped; component and
    # doc chunks describe shared code and shared docs. Enforced in the SQL query,
    # never by prompting the model to ignore rows (#24).
    visibility: str = TEAM


@dataclass(frozen=True)
class Hit:
    """A chunk as returned by a retriever, with the scores that put it there.

    Scores are kept per-arm rather than collapsed to one number: the Part 1
    writeup has to say *which* stage fixed a query, and a single fused score
    cannot answer that.
    """

    chunk: Chunk
    rank: int                      # 1-based, as the retriever returned it
    dense_score: Optional[float] = None
    bm25_score: Optional[float] = None
    rrf_score: Optional[float] = None
    rerank_score: Optional[float] = None

    @property
    def chunk_id(self) -> str:
        return self.chunk.chunk_id


@dataclass(frozen=True)
class Anchor:
    """A golden reference in an eval case, resolved to chunk ids at load time.

    Eval cases store anchors, not chunk ids, because ids move every time the
    corpus is re-chunked and a golden set that silently goes stale is worse than
    no golden set. An anchor names something durable — a decision number, a
    heading, a symbol — and the harness resolves it against the live index.

    A case with NO anchors is the out-of-corpus case: deliberately unanswerable,
    correct as written. Its metrics are undefined rather than zero, and it is
    excluded from averages and counted separately.
    """

    kind: ChunkKind
    # Interpreted per kind: for 'decision' the record number, for 'doc' the
    # heading path, for 'component' the symbol_uid (optionally plus `symbol`).
    ref: str
    symbol: Optional[str] = None


@dataclass(frozen=True)
class EvalCase:
    """One row of the eval set."""

    case_id: str
    query: str
    category: str                  # a Session 11 §5 failure category
    golden_answer: str = ""
    anchors: tuple[Anchor, ...] = field(default_factory=tuple)

    @property
    def out_of_corpus(self) -> bool:
        """True when the corpus genuinely cannot answer this."""
        return not self.anchors
