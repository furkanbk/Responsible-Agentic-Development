"""eval.trace_adapters — a stored trace, adapted into what HW5's scorers already eat.

Owner: Alejandro Ramírez Trueba (Phase 15C, Part 2.3).

The scorers do not change. `eval/retrieval_metrics.py`, `eval/generation_metrics.py`
(both mine) and `monitor/judge.py` (Dias's) stay byte-identical this phase — the diff
is new files only. If wiring a scorer to a trace made me edit the scorer, the adapter
would be doing too little. So the whole join between "a trace, read back as data"
(`tracing.trajectory`, contract #13) and "a scorer's input shape" lives here.

Two adapters, because there are two scorer input shapes:

  * ``trace_to_rag_inputs`` -> ``{question, answer, chunks, ranked_ids}`` — what the
    RAG generation scorers and the five rank metrics consume. This is the adapter the
    assignment says has no worked example: Session 12 fed a hand-rolled judge; ours has
    to rebuild `retrieval.types.Chunk` from what the RETRIEVER span recorded and feed
    what HW5 actually shipped.
  * ``trace_to_run_record`` -> a dict in ``RunLog.to_dict()`` shape, so ``judge_run``
    runs over a trace with **zero edits**.

**Retriever order, never packed order.** `retrieved_chunks` already returns the
RETRIEVER's ranking (decisions #59/#91), and `ranked_ids_from_hits` re-sorts on
`Hit.rank` so a dict round-trip on the way through a span cannot quietly feed MRR and
nDCG the wrong order. That function is half of the first adapter; it is reused, not
reimplemented.

**`assembled.instructions` is the one field a span tree cannot carry.** The judge's
whole point is telling "ignored the rule" apart from "never had the rule", and an
empty string quietly answers "never had it". So instructions come from the joined
`runs.jsonl` record (via the `radf.run_id` tag), and when the join misses this adapter
sets ``instructions=None`` and marks ``instructions_source="missing"`` — it says so
rather than passing ``""``. The batch driver then skips the judge for that trace
instead of feeding it a misleading blank.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from retrieval.types import KINDS, TEAM, Chunk, Hit
from eval.retrieval_metrics import ranked_ids_from_hits
from tracing.trajectory import (
    retrieved_chunks,
    tool_branches,
    tool_calls_from_trace,
    trace_answer,
    trace_request,
    trace_stopped,
)

# The RunLog fields a span tree genuinely carries. Everything else on a run record
# (assembled context, envelopes, applied changes) comes from the joined runs.jsonl.
_TRACE_TAG_RUN_ID = "radf.run_id"
_TRACE_TAG_AGENT = "radf.agent"
_TRACE_TAG_USER = "radf.user_id"


# --- trace -> RAG scorer inputs -----------------------------------------------

def _chunk_from_dict(d: dict[str, Any]) -> Chunk:
    """Rebuild a frozen `Chunk` from the plain dict a RETRIEVER span recorded.

    Only `chunk_id` is required to be useful downstream (it is what the rank
    metrics read); every other field falls back to the dataclass default, so a
    lean span that only stored ids and ranks still produces valid chunks. An
    unrecognised `kind` is coerced to `"doc"` — doc chunks are the unscoped kind
    (#24), so this can never widen visibility, and it keeps a malformed span from
    crashing a whole batch pass.
    """
    kind = d.get("kind")
    if kind not in KINDS:
        kind = "doc"
    return Chunk(
        chunk_id=str(d.get("chunk_id", "")),
        kind=kind,
        text=str(d.get("text", "")),
        symbol_uid=d.get("symbol_uid"),
        symbol=d.get("symbol"),
        heading_path=str(d.get("heading_path", "")),
        source_path=str(d.get("source_path", "")),
        content_sha=str(d.get("content_sha", "")),
        visibility=str(d.get("visibility", TEAM)),
    )


def _hit_from_dict(d: dict[str, Any], fallback_rank: int) -> Hit:
    """A `Hit` from a recorded chunk dict. `rank` falls back to list position.

    The span records `rank`, but a span that omitted it should still rank in the
    order the retriever returned rather than collapse every hit to rank 0 (which
    would make MRR read the wrong position). `fallback_rank` is 1-based.
    """
    rank = d.get("rank")
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        rank = fallback_rank
    return Hit(
        chunk=_chunk_from_dict(d),
        rank=rank,
        dense_score=d.get("dense_score"),
        bm25_score=d.get("bm25_score"),
        rrf_score=d.get("rrf_score"),
        rerank_score=d.get("rerank_score"),
    )


def hits_from_trace(trace: Any) -> list[Hit]:
    """The run's retrieved chunks as `Hit`s, in retriever order (by `Hit.rank`)."""
    dicts = retrieved_chunks(trace)
    hits = [_hit_from_dict(d, i + 1) for i, d in enumerate(dicts)]
    return sorted(hits, key=lambda h: (h.rank, h.chunk_id))


def trace_to_rag_inputs(trace: Any) -> dict[str, Any]:
    """`{question, answer, chunks, ranked_ids}` for the RAG and rank scorers.

    `chunks` are `retrieval.types.Chunk` in retriever order — exactly what
    `generation_metrics.score_case` takes. `ranked_ids` is the same order as a
    plain id list for the five rank metrics, produced by the frozen
    `ranked_ids_from_hits` so the two orders can never disagree.
    """
    hits = hits_from_trace(trace)
    return {
        "question": trace_request(trace),
        "answer": trace_answer(trace),
        "chunks": [h.chunk for h in hits],
        "ranked_ids": ranked_ids_from_hits(hits),
    }


# --- trace -> run record (for the monitor judge) ------------------------------

def _trace_tags(trace: Any) -> dict[str, str]:
    info = getattr(trace, "info", None)
    tags = getattr(info, "tags", None)
    return dict(tags) if isinstance(tags, dict) else {}


def _trace_id(trace: Any) -> Optional[str]:
    info = getattr(trace, "info", None)
    return getattr(info, "trace_id", None)


def _steps_from_trace(trace: Any) -> list[dict[str, Any]]:
    """The tool steps a span tree carries: name, arguments, and the guard branch.

    Arguments come from `tool_calls_from_trace` (contract #13: what the agent
    *tried*, never the result), the branch from `tool_branches` (the guards'
    decision, recorded at dispatch). Both iterate TOOL spans in the same
    start-time order, so they zip positionally.
    """
    calls = tool_calls_from_trace(trace)
    branches = tool_branches(trace)
    steps: list[dict[str, Any]] = []
    for i, call in enumerate(calls):
        branch = branches[i].get("branch") if i < len(branches) else None
        steps.append({"tool": call.tool, "args": call.arguments, "branch": branch})
    return steps


def load_runs_index(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """`{run_id: record}` from `store/runs/runs.jsonl`, for the instructions join.

    Respects `RADF_RUNS_DIR` through `agentlib.runlog.runs_file`, so a test's
    isolated store is read instead of the developer's real log. A missing or
    unreadable file is an empty index, not an error — the caller reports every
    run whose instructions it could not find.
    """
    if path is None:
        from agentlib.runlog import runs_file

        path = runs_file()
    index: dict[str, dict[str, Any]] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return index
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        run_id = record.get("run_id")
        if run_id:
            index[str(run_id)] = record
    return index


def trace_to_run_record(
    trace: Any, *, runs_index: Optional[dict[str, dict[str, Any]]] = None
) -> dict[str, Any]:
    """A `RunLog.to_dict()`-shaped record so `judge_run(record)` runs unedited.

    request / answer / stopped / steps are reconstructed from the trace; the
    assembled context, envelopes and applied changes are joined from the
    `runs.jsonl` record named by the `radf.run_id` tag. When that record is
    missing, `assembled.instructions` is `None` and `instructions_source` is
    `"missing"` — never `""`, which the judge would misread as "the rule was
    never in context".
    """
    if runs_index is None:
        runs_index = load_runs_index()
    tags = _trace_tags(trace)
    run_id = tags.get(_TRACE_TAG_RUN_ID)
    joined = runs_index.get(run_id) if run_id else None

    if joined is not None:
        assembled = dict(joined.get("assembled") or {})
        assembled.setdefault("instructions", None)
        assembled.setdefault("data_blocks", [])
        assembled.setdefault("sources", [])
        instructions_source = "runs.jsonl" if assembled.get("instructions") is not None else "missing"
        envelopes = joined.get("envelopes", [])
        applied_changes = joined.get("applied_changes", [])
        scratch = joined.get("scratch", {})
    else:
        assembled = {"instructions": None, "data_blocks": [], "sources": []}
        instructions_source = "missing"
        envelopes = []
        applied_changes = []
        scratch = {}

    return {
        "run_id": run_id,
        "agent": tags.get(_TRACE_TAG_AGENT),
        "user_id": tags.get(_TRACE_TAG_USER),
        "thread_id": (joined or {}).get("thread_id"),
        "request": trace_request(trace),
        "started_at": (joined or {}).get("started_at"),
        "ended_at": (joined or {}).get("ended_at"),
        "assembled": assembled,
        "steps": _steps_from_trace(trace),
        "envelopes": envelopes,
        "applied_changes": applied_changes,
        "scratch": scratch,
        "stopped": trace_stopped(trace),
        "answer": trace_answer(trace),
        "trace_id": _trace_id(trace),
        # Not a RunLog field — a provenance flag the batch driver reads to decide
        # whether the monitor judge can run on this trace at all.
        "instructions_source": instructions_source,
    }


__all__ = [
    "hits_from_trace",
    "trace_to_rag_inputs",
    "trace_to_run_record",
    "load_runs_index",
]
