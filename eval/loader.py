"""eval.loader — eval cases + anchor resolution (HW5, T14.10).

Owner: Alejandro Ramírez Trueba (Phase 14C, Part 2).

`eval/cases.json` stores **content anchors, not chunk ids** (contract #10). Ids are
`sha1(identity)[:16]` and move every time the corpus is re-chunked differently, so a
golden set pinned to ids goes stale silently the first time the chunker changes — a
golden that quietly points at nothing is worse than no golden at all. An anchor names
something durable instead — a decision number, a symbol, a heading — and this module
resolves it against the *live* index at load time.

Resolution per anchor kind (the `ref` column is interpreted per kind, as `Anchor`
documents):

  * **component** — `ref` is a dotted module path; `resolve_uid(ref)` gives the
    `symbol_uid`. With `symbol` set, the golden is that symbol's card; without it,
    the module card (the chunk whose `symbol` is empty).
  * **decision** — `ref` is the decision's *record number* (`"21"`). Decisions #1..#65
    live as one-row-per-chunk `doc` chunks of `docs/ARCHITECTURE.md`'s §5 decision
    log (the chunker keeps each table row atomic), so this matches the doc chunk whose
    first table cell is that number. Resolving by number, not by id or by wording,
    is what keeps the golden stable across a re-chunk.
  * **doc** — `ref` is a heading-path fragment (`"5. Decision log"`, `"Purpose"`);
    matches every `doc` chunk whose `heading_path` contains it. A section split into
    several chunks resolves to all of them, and all are golden.

An anchor that resolves to nothing is surfaced, never swallowed: `resolve_case`
returns the unresolved anchors alongside the golden ids, and the harness reports
them. A case whose anchors *all* fail to resolve is a broken golden — a different
thing from an out-of-corpus case (which declares no anchors on purpose) — and is
excluded from the averages and counted under its own heading, so a stale eval set
shows up as a number instead of as silently wrong metrics.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Optional, Sequence

from retrieval.types import Anchor, Chunk, EvalCase
from overlay.uid import resolve_uid

_HERE = Path(__file__).resolve().parent
DEFAULT_CASES = _HERE / "cases.json"

# A decision-log doc chunk carries the row `| 21 | 2026-... | component | ... |`.
# The number is the first table cell; anchor the match to that so `#21` never
# matches "21" appearing inside another row's prose.
_DECISION_ROW = re.compile(r"^\s*\|\s*(\d+)\s*\|")


def load_cases(path: Optional[Path] = None) -> list[EvalCase]:
    """Parse `cases.json` into frozen `EvalCase`s. Resolution is a separate step.

    Loading does not touch the index — a case is well-formed on its own, and
    keeping parse separate from resolve lets the offline tests read the real eval
    set without a Postgres running.
    """
    raw = json.loads(Path(path or DEFAULT_CASES).read_text(encoding="utf-8"))
    cases: list[EvalCase] = []
    for row in raw:
        anchors = tuple(
            Anchor(kind=a["kind"], ref=str(a["ref"]), symbol=a.get("symbol"))
            for a in row.get("anchors", [])
        )
        cases.append(
            EvalCase(
                case_id=row["case_id"],
                query=row["query"],
                category=row["category"],
                golden_answer=row.get("golden_answer", ""),
                anchors=anchors,
            )
        )
    return cases


def _decision_row_number(chunk: Chunk) -> Optional[str]:
    """The decision record number a doc chunk carries, if it is a decision-log row."""
    if chunk.kind != "doc" or "decision log" not in chunk.heading_path.lower():
        return None
    for line in chunk.text.splitlines():
        m = _DECISION_ROW.match(line)
        if m:
            return m.group(1)
    return None


def resolve_anchor(anchor: Anchor, corpus: Sequence[Chunk]) -> set[str]:
    """The chunk ids in `corpus` this anchor names. Empty set means unresolved."""
    if anchor.kind == "component":
        uid = resolve_uid(anchor.ref)
        if not uid:
            return set()
        out = set()
        for c in corpus:
            if c.kind != "component" or c.symbol_uid != uid:
                continue
            if anchor.symbol:
                if c.symbol == anchor.symbol:
                    out.add(c.chunk_id)
            elif not c.symbol:                 # the module card
                out.add(c.chunk_id)
        return out

    if anchor.kind == "decision":
        want = str(anchor.ref).lstrip("#").strip()
        return {c.chunk_id for c in corpus if _decision_row_number(c) == want}

    if anchor.kind == "doc":
        needle = anchor.ref.lower()
        return {
            c.chunk_id
            for c in corpus
            if c.kind == "doc" and needle in c.heading_path.lower()
        }

    return set()


def resolve_case(case: EvalCase, corpus: Sequence[Chunk]) -> tuple[set[str], list[Anchor]]:
    """`(golden chunk ids, anchors that resolved to nothing)`.

    The golden set is the union over the case's anchors. Unresolved anchors are
    returned rather than dropped so the harness can report a stale eval set as a
    count instead of letting it depress recall invisibly.
    """
    golden: set[str] = set()
    unresolved: list[Anchor] = []
    for anchor in case.anchors:
        ids = resolve_anchor(anchor, corpus)
        if ids:
            golden |= ids
        else:
            unresolved.append(anchor)
    return golden, unresolved


class ResolvedCase:
    """An `EvalCase` joined to the live index: its golden ids and any misses.

    `status` is the harness's scoring decision, made once here so the metric loop
    and the report agree:

      * ``"scored"``       — anchors declared and at least one resolved; metrics apply.
      * ``"out_of_corpus"`` — no anchors declared; deliberately unanswerable, metrics
                              undefined, counted separately (assignment Part 2).
      * ``"unresolved"``    — anchors declared but none resolved; a broken/stale
                              golden, excluded and counted under its own heading so it
                              is visible rather than scored as a retrieval failure.
    """

    __slots__ = ("case", "golden", "unresolved")

    def __init__(self, case: EvalCase, golden: set[str], unresolved: list[Anchor]):
        self.case = case
        self.golden = golden
        self.unresolved = unresolved

    @property
    def status(self) -> str:
        if self.case.out_of_corpus:
            return "out_of_corpus"
        if not self.golden:
            return "unresolved"
        return "scored"


def resolve_all(cases: Iterable[EvalCase], corpus: Sequence[Chunk]) -> list[ResolvedCase]:
    """Resolve every case against one snapshot of the corpus."""
    out = []
    for case in cases:
        golden, unresolved = resolve_case(case, corpus)
        out.append(ResolvedCase(case, golden, unresolved))
    return out


__all__ = [
    "DEFAULT_CASES",
    "load_cases",
    "resolve_anchor",
    "resolve_case",
    "resolve_all",
    "ResolvedCase",
]
