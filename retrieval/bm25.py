"""retrieval.bm25 — Okapi BM25, hand-written.

Owner: Berat Furkan Kocak (HW5, T14.6a).

**Why this is not `ts_rank_cd` and not `rank_bm25`** (decision #58).

Postgres full-text ranking looked free — both retrieval arms in one SQL query —
and it is not BM25. `ts_rank`/`ts_rank_cd` consult no corpus-wide document
frequency at all, so there is **no IDF**, and IDF is the entire mechanism by
which the lexical arm beats dense retrieval on the queries it was added for.
This corpus is full of rare identifiers — `impact_scope`, `prune_graph_node`,
`symbol_uid` — and without IDF each of them weighs the same as `agent`, which
appears in nearly every chunk.

The missing `k1` saturation bites just as concretely: `docs/TODO.md`'s ownership
tables repeat the same names dozens of times, and a ranker linear in term
frequency floats them to the top of half the queries. BM25 saturates tf, so the
thirtieth occurrence of "Alejandro" adds almost nothing.

`rank_bm25` was rejected for the reason CLAUDE.md §4 rejects any convenience
dependency: BM25 is a formula, this is forty lines of it, and importing a ranker
removes the thing HW5 is graded on.

Parameters, chosen rather than inherited:

    k1 = 1.2   tf saturation. The literature default, and right here: our chunks
               are short (<=1200 chars) and a term repeating 3+ times in one is
               already a strong signal that should not keep growing.
    b  = 0.75  length normalization. Also the default, and deliberately NOT 0 —
               chunk lengths in this corpus vary by an order of magnitude (a
               one-line symbol card vs a full decision record), which is exactly
               the situation length normalization exists for.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, Sequence

K1 = 1.2
B = 0.75

# Same character class as `overlay.memory._tokens` — identifiers stay whole, so
# `symbol_uid` is one term rather than two, and `agentlib/core.py` survives as a
# searchable string. Deliberately NOT that function: it returns a `set` (it is
# for cue matching), and BM25 needs term frequencies.
_WORD = re.compile(r"[a-z0-9_./]+")


def tokenize(text: str) -> list[str]:
    """Lowercase token list, preserving repeats (BM25 needs tf)."""
    return _WORD.findall((text or "").lower())


class BM25:
    """A BM25 index over an in-memory corpus.

    Built per query batch rather than persisted: at ~600 chunks the whole index
    is a few hundred KB and builds in milliseconds, so a stale-index bug costs
    more than the rebuild ever would.
    """

    def __init__(self, docs: Sequence[tuple[str, str]], *, k1: float = K1, b: float = B):
        """`docs` is a sequence of `(doc_id, text)`."""
        self.k1 = k1
        self.b = b
        self.doc_ids: list[str] = []
        self.tfs: list[Counter] = []
        self.lengths: list[int] = []

        df: Counter = Counter()
        for doc_id, text in docs:
            toks = tokenize(text)
            tf = Counter(toks)
            self.doc_ids.append(doc_id)
            self.tfs.append(tf)
            self.lengths.append(len(toks))
            df.update(tf.keys())

        self.n = len(self.doc_ids)
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0
        # Robertson/Sparck-Jones IDF with the +1 inside the log, so a term
        # present in every document scores a small positive value rather than a
        # negative one. The unsmoothed form can go negative and let a stopword
        # actively push a document DOWN, which is not what "this term is
        # uninformative" should mean.
        self.idf: dict[str, float] = {
            term: math.log(1.0 + (self.n - n_q + 0.5) / (n_q + 0.5))
            for term, n_q in df.items()
        }

    def score(self, query: str | Iterable[str]) -> dict[str, float]:
        """Score every document against `query`. Returns `{doc_id: score}`.

        Documents matching no query term are omitted rather than returned at
        0.0 — the caller ranks and truncates, and carrying hundreds of zeros
        just to drop them is noise in every debug dump downstream.
        """
        terms = tokenize(query) if isinstance(query, str) else list(query)
        if not terms or not self.n:
            return {}

        scores: dict[str, float] = {}
        for i, doc_id in enumerate(self.doc_ids):
            tf = self.tfs[i]
            dl = self.lengths[i]
            norm = self.k1 * (1 - self.b + self.b * (dl / self.avgdl if self.avgdl else 0.0))
            total = 0.0
            for term in terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                total += self.idf.get(term, 0.0) * (f * (self.k1 + 1)) / (f + norm)
            if total > 0.0:
                scores[doc_id] = total
        return scores

    def top(self, query: str, n: int) -> list[tuple[str, float]]:
        """The `n` highest-scoring documents, descending."""
        ranked = sorted(self.score(query).items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:n]
