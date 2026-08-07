"""eval.run_trace_scoring — HW5's scorers, run over stored MLflow traces (T15.11).

Owner: Alejandro Ramírez Trueba (Phase 15C, Part 2.3).

A batch pass, not a background worker: the assignment is explicit that a pass over
already-stored traces satisfies Part 2.3, and asyncio would be scope no one asked for.
It finds traces by tag (`request_origin=batch` for the eval set), adapts each into the
shape its scorers already take (`eval.trace_adapters`), runs the **unchanged** scorers,
and writes each result back onto the trace with `mlflow.log_feedback` under the
namespaces contract #14 fixes so 15C and 15D never collide:

    retrieval.*    the five rank metrics          (needs golden ids -> the live index)
    generation.*   HW5's judged RAG metrics       (question / answer / chunks off the trace)
    monitor.*      Dias's after-the-fact judge     (a run record off the trace + runs.jsonl)

**Which scorer runs over which trace depends on what the trace can supply, and that
decision lives here, not in a scorer.** The judged RAG metrics are reference-free and
run over any trace with an answer; context recall and the five rank metrics need a
golden, so they run only over eval-set traces (which carry `eval_case_id`) and, for
the rank metrics, only when the index is up to resolve anchors to ids. When it is not,
`retrieval.*` is skipped with a printed note rather than invented — exactly how
`run_eval` degrades — and `generation.*`/`monitor.*`, which need no database, still run.

**Flush before reading.** MLflow logs traces asynchronously; `find_traces` flushes
first (see `tracing.setup.flush`), and so does this module before the pass. Without it
the first read after a run comes back empty and reads like a broken trace (T15.2).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from eval import generation_metrics as gm
from eval import retrieval_metrics as rm
from eval.loader import load_cases
from eval.trace_adapters import (
    load_runs_index,
    trace_to_rag_inputs,
    trace_to_run_record,
)
from retrieval.types import Anchor, EvalCase

# The three depths HW5's retrieval matrix reports at (run_eval.KS). Same k's here so a
# feedback value on a trace lines up with the offline table row for row.
KS: tuple[int, ...] = (3, 5, 10)

# The judged RAG metrics that need no reference answer — the subset that can run over
# an interactive trace that never came from an eval case.
_REFERENCE_FREE = ("faithfulness", "answer_relevance", "context_precision")

_HERE = Path(__file__).resolve().parent
_GEN_CASES = _HERE / "gen_cases.json"


@dataclass
class Feedback:
    """One metric value destined for one trace, under its contract-#14 name."""

    name: str
    value: Any
    rationale: str = ""


#: A feedback writer: `(trace_id, name, value, rationale) -> None`. Injected so the
#: batch pass is testable without MLflow and printable in `--dry-run`.
Sink = Callable[[str, str, Any, str], None]


# --- cases (base + the generation extension) ----------------------------------

def load_cases_by_id(base: Optional[Path] = None) -> dict[str, EvalCase]:
    """`{case_id: EvalCase}` over the 24 base cases plus `gen_cases.json`'s six.

    `gen_cases.json` extends `cases.json` (Dias, T14.14) rather than duplicating
    it, so a trace tagged with a generation-only case id (`g0*`) still finds its
    golden answer here. The base loader parses the list form; the extension is a
    dict whose `cases` key holds rows in the same shape.
    """
    cases = {c.case_id: c for c in load_cases(base)}
    try:
        data = json.loads(_GEN_CASES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cases
    for row in data.get("cases", []):
        anchors = tuple(
            Anchor(kind=a["kind"], ref=str(a["ref"]), symbol=a.get("symbol"))
            for a in row.get("anchors", [])
        )
        cases[row["case_id"]] = EvalCase(
            case_id=row["case_id"],
            query=row["query"],
            category=row["category"],
            golden_answer=row.get("golden_answer", ""),
            anchors=anchors,
        )
    return cases


# --- scoring one trace --------------------------------------------------------

def _retrieval_feedbacks(ranked_ids: Sequence[str], golden: set[str]) -> list[Feedback]:
    """The five rank metrics at each k, skipping any the case leaves undefined.

    A `None` (empty golden / out-of-corpus) is *not* logged as 0.0 — decision #67
    keeps it undefined and excluded, and a 0.0 on a trace would read as a real
    retrieval failure in any later aggregation over feedbacks.
    """
    out: list[Feedback] = []
    for k in KS:
        for name, value in rm.metrics_for_case(ranked_ids, golden, k).items():
            if value is not None:
                out.append(Feedback(f"retrieval.{name}_at_{k}", value))
    return out


def _generation_feedbacks(
    question: str,
    answer: str,
    chunks: Sequence,
    *,
    case: Optional[EvalCase],
    model: str,
    use_cache: bool,
) -> list[Feedback]:
    """HW5's judged RAG metrics. Full `score_case` for an eval case; the
    reference-free subset for a trace that never had a golden answer."""
    if case is not None:
        results = gm.score_case(
            question, answer, chunks, case.golden_answer,
            out_of_corpus=case.out_of_corpus, model=model, use_cache=use_cache,
        )
    else:
        results = {
            "faithfulness": gm.faithfulness(answer, chunks, model=model, use_cache=use_cache),
            "answer_relevance": gm.answer_relevance(question, answer, model=model, use_cache=use_cache),
            "context_precision": gm.context_precision(question, chunks, model=model, use_cache=use_cache),
        }
    out: list[Feedback] = []
    for mr in results.values():
        if mr.score is not None:
            out.append(Feedback(f"generation.{mr.name}", mr.score, mr.notes))
    return out


def _monitor_feedbacks(record: dict, *, model: Optional[str]) -> list[Feedback]:
    """Dias's judge over a run record rebuilt from the trace.

    Skipped — never guessed — when the run's instructions could not be joined
    from `runs.jsonl`: judging adherence to a rule set we cannot prove was in
    context is exactly the confusion the judge exists to avoid."""
    if record.get("instructions_source") == "missing":
        return []
    from monitor.judge import judge_run

    verdict = judge_run(record) if model is None else judge_run(record, model=model)
    if not verdict.gradeable:
        return []
    return [
        Feedback("monitor.adherence", verdict.prompt_adherence),
        Feedback("monitor.grounding", verdict.grounding),
        Feedback("monitor.is_problem", verdict.is_problem),
    ]


def feedbacks_for_trace(
    trace: Any,
    *,
    case: Optional[EvalCase] = None,
    golden: Optional[set[str]] = None,
    runs_index: Optional[dict[str, dict]] = None,
    run_record: Optional[dict] = None,
    model: str = gm.STRONG,
    use_cache: bool = True,
    run_retrieval: bool = True,
    run_generation: bool = True,
    run_monitor: bool = True,
) -> list[Feedback]:
    """Every feedback one trace earns, across the three families.

    Pure of MLflow: it computes values, it does not write them. The retrieval
    family is the only one testable without a model, so the three families are
    independently switchable — the offline suite drives retrieval + the missing
    instructions skip, the online test drives all three. `run_record` may be
    passed in to avoid rebuilding it when the caller already has it.
    """
    rag = trace_to_rag_inputs(trace)
    out: list[Feedback] = []

    if run_retrieval and golden is not None:
        out += _retrieval_feedbacks(rag["ranked_ids"], golden)

    answer = rag["answer"]
    if run_generation and answer:
        question = case.query if case is not None else rag["question"]
        out += _generation_feedbacks(
            question, answer, rag["chunks"],
            case=case, model=model, use_cache=use_cache,
        )

    if run_monitor:
        if run_record is None:
            run_record = trace_to_run_record(
                trace, runs_index=runs_index if runs_index is not None else {})
        out += _monitor_feedbacks(run_record, model=None)

    return out


# --- the batch pass -----------------------------------------------------------

def _trace_tags(trace: Any) -> dict[str, str]:
    info = getattr(trace, "info", None)
    tags = getattr(info, "tags", None)
    return dict(tags) if isinstance(tags, dict) else {}


def _trace_id(trace: Any) -> Optional[str]:
    info = getattr(trace, "info", None)
    return getattr(info, "trace_id", None)


def _default_sink(trace_id: str, name: str, value: Any, rationale: str) -> None:
    """Write one feedback onto a trace with MLflow."""
    import mlflow

    mlflow.log_feedback(
        trace_id=trace_id, name=name, value=value, rationale=rationale or None
    )


def _dry_run_sink(trace_id: str, name: str, value: Any, rationale: str) -> None:
    short = (trace_id or "?")[:14]
    print(f"  [{short}] {name} = {value}")


def _resolve_golden(cases_by_id: dict[str, EvalCase]) -> Optional[dict[str, set[str]]]:
    """`{case_id: golden ids}` resolved against the live index, or None if it is down.

    The one part that needs Postgres, kept behind a try like `run_eval`'s CLI so a
    machine with no database still writes `generation.*` and `monitor.*`.
    """
    try:
        from retrieval.store import IndexUnavailable, all_chunks, connect
        from eval.loader import resolve_all
    except Exception as exc:  # noqa: BLE001
        print(f"retrieval layer unavailable ({exc}); skipping retrieval.* feedback",
              file=sys.stderr)
        return None
    try:
        with connect() as conn:
            corpus = all_chunks(conn)
    except IndexUnavailable as exc:
        print(f"retrieval index unavailable ({exc}); skipping retrieval.* feedback",
              file=sys.stderr)
        return None
    if not corpus:
        print("index is empty; skipping retrieval.* feedback", file=sys.stderr)
        return None
    resolved = resolve_all(cases_by_id.values(), corpus)
    return {rc.case.case_id: rc.golden for rc in resolved if rc.status == "scored"}


def run(
    *,
    request_origin: Optional[str] = "batch",
    limit: int = 200,
    model: str = gm.STRONG,
    resolve_golden: bool = True,
    dry_run: bool = False,
    sink: Optional[Sink] = None,
    run_generation: bool = True,
    run_monitor: bool = True,
) -> dict[str, int]:
    """Score every matching stored trace and write the feedback back.

    Returns counts (`traces`, `feedbacks`, `skipped_no_instructions`) for the
    caller and the report. `sink` defaults to MLflow, or to a printing sink under
    `--dry-run`; tests pass a collector.
    """
    from tracing.setup import flush, init_tracing, tracing_enabled
    from tracing.trajectory import find_traces

    init_tracing()
    if not tracing_enabled():
        print("tracing is off (mlflow not installed or init failed); nothing to score",
              file=sys.stderr)
        return {"traces": 0, "feedbacks": 0, "skipped_no_instructions": 0}

    flush()
    traces = find_traces(request_origin=request_origin, limit=limit)
    cases_by_id = load_cases_by_id()
    golden_by_case = _resolve_golden(cases_by_id) if resolve_golden else None
    runs_index = load_runs_index()
    write = sink or (_dry_run_sink if dry_run else _default_sink)

    counts = {"traces": 0, "feedbacks": 0, "skipped_no_instructions": 0}
    for trace in traces:
        counts["traces"] += 1
        tags = _trace_tags(trace)
        case_id = tags.get("eval_case_id")
        case = cases_by_id.get(case_id) if case_id else None
        golden = golden_by_case.get(case_id) if (golden_by_case and case_id) else None
        trace_id = _trace_id(trace)

        record = trace_to_run_record(trace, runs_index=runs_index) if run_monitor else None
        if run_monitor and record.get("instructions_source") == "missing":
            counts["skipped_no_instructions"] += 1

        fbs = feedbacks_for_trace(
            trace, case=case, golden=golden, runs_index=runs_index, run_record=record,
            model=model, run_retrieval=golden is not None,
            run_generation=run_generation, run_monitor=run_monitor,
        )
        for fb in fbs:
            write(trace_id, fb.name, fb.value, fb.rationale)
            counts["feedbacks"] += 1

    return counts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="HW6 Part 2.3 — run HW5's scorers over stored traces, write feedback back")
    parser.add_argument("--origin", default="batch",
                        help="request_origin to score (default: batch = the eval set)")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--model", default=gm.STRONG)
    parser.add_argument("--no-golden", action="store_true",
                        help="skip retrieval.* (do not touch the index)")
    parser.add_argument("--no-generation", action="store_true")
    parser.add_argument("--no-monitor", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the feedback instead of writing it to MLflow")
    args = parser.parse_args(argv)

    counts = run(
        request_origin=args.origin, limit=args.limit, model=args.model,
        resolve_golden=not args.no_golden, dry_run=args.dry_run,
        run_generation=not args.no_generation, run_monitor=not args.no_monitor,
    )
    print(f"\nscored {counts['traces']} traces, wrote {counts['feedbacks']} feedbacks"
          f" ({counts['skipped_no_instructions']} skipped: no joined instructions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
