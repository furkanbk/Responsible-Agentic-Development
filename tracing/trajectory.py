"""tracing.trajectory — a trace, read back as data (T15.4).

Owner: Berat Furkan Kocak. Contract **#13** (docs/TODO.md, HW6).

This is the twenty lines the assignment promises, plus the accessors the two
parallel tracks need so neither has to learn MLflow's span API:

    tool_calls_from_trace  ordered list[ToolCall]   -> Part 1 metrics (15B)
    trace_request/answer   the root span's I/O      -> RAG scorers (15C)
    retrieved_chunks       RETRIEVER-order hits     -> RAG scorers (15C)
    llm_calls              model/usage/latency      -> cost reporting
    find_traces            search by tag            -> batch passes (15C, 15D)

**Arguments, never results.** A trajectory is what the agent *tried*. Folding a
tool's output into it makes a correct call look wrong the moment a tool
legitimately errors — and an expected-tool-calls comparison is about selection
and parameters, not about whether the graph happened to contain the node. The
spans do record outputs; this adapter ignores them. 15D's indirect-injection
detector is the caller that wants them, and it reads the spans directly.

**Retriever order, not packed order.** `retrieved_chunks` returns what `search()`
ranked, because `retrieval/search.py` records the span before `pack_for_llm`
reorders for the context window (decisions #59, #91). Two of HW5's five rank
metrics silently degrade if this is ever "fixed" to return what the model saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .setup import flush, tracing_enabled
from .spans import TOOL_NAME_ATTR


@dataclass(frozen=True)
class ToolCall:
    """One attempted tool call: the name, and the arguments it was called with."""

    tool: str
    arguments: dict[str, Any]

    def __str__(self) -> str:  # readable in a failure report
        args = ", ".join(f"{k}={v!r}" for k, v in sorted(self.arguments.items()))
        return f"{self.tool}({args})"


# --- reading one trace --------------------------------------------------------

def _spans(trace: Any, span_type: Optional[str] = None) -> list[Any]:
    """Spans of a type, in start-time order. Missing/odd traces give []."""
    try:
        spans = list(trace.data.spans or [])
    except Exception:  # noqa: BLE001
        return []
    if span_type is not None:
        spans = [s for s in spans if getattr(s, "span_type", None) == span_type]
    return sorted(spans, key=lambda s: getattr(s, "start_time_ns", 0) or 0)


def tool_calls_from_trace(trace: Any) -> list[ToolCall]:
    """The run's trajectory: TOOL spans, start-time order, name + arguments.

    The name comes from the `gen_ai.tool.name` attribute rather than the span
    name, so renaming a span for readability never breaks the eval set.
    """
    calls: list[ToolCall] = []
    for span in _spans(trace, "TOOL"):
        attributes = getattr(span, "attributes", None) or {}
        name = attributes.get(TOOL_NAME_ATTR)
        if not name:
            continue
        inputs = getattr(span, "inputs", None)
        calls.append(ToolCall(tool=str(name),
                              arguments=dict(inputs) if isinstance(inputs, dict) else {}))
    return calls


def root_span(trace: Any) -> Optional[Any]:
    """The trace's root — the agent invocation. None if the trace is unreadable."""
    spans = _spans(trace)
    if not spans:
        return None
    for span in spans:
        if getattr(span, "parent_id", None) in (None, ""):
            return span
    return spans[0]


def trace_request(trace: Any) -> str:
    """The user request the run started from, off the root span's inputs."""
    span = root_span(trace)
    inputs = getattr(span, "inputs", None) if span is not None else None
    if isinstance(inputs, dict):
        for key in ("request", "query", "user_msg", "input"):
            if inputs.get(key):
                return str(inputs[key])
    return ""


def trace_answer(trace: Any) -> Optional[str]:
    """The run's final answer, off the root span's outputs. None if it never got one."""
    span = root_span(trace)
    outputs = getattr(span, "outputs", None) if span is not None else None
    if isinstance(outputs, dict):
        for key in ("answer", "output", "result"):
            if outputs.get(key) is not None:
                return str(outputs[key])
    return None


def trace_stopped(trace: Any) -> Optional[str]:
    """The stopping condition the CODE chose (`answered`, `max_steps`, ...).

    On the root span's outputs because the eval's goal check reads it, and
    "stopped for a reason" is the loop's decision, never the model's (§5).
    """
    span = root_span(trace)
    outputs = getattr(span, "outputs", None) if span is not None else None
    if isinstance(outputs, dict) and outputs.get("stopped"):
        return str(outputs["stopped"])
    return None


def retrieved_chunks(trace: Any) -> list[dict[str, Any]]:
    """Every retrieved chunk across the run's RETRIEVER spans, in retriever order.

    Returns the plain dicts the span recorded (`chunk_id`, `rank`, `text`, the
    per-arm scores, ...). 15C rebuilds `retrieval.types.Chunk` from these; this
    module deliberately does not import `retrieval` — the safety track has no
    reason to pull a pgvector client in to read a trace.
    """
    out: list[dict[str, Any]] = []
    for span in _spans(trace, "RETRIEVER"):
        outputs = getattr(span, "outputs", None)
        hits = outputs.get("hits") if isinstance(outputs, dict) else outputs
        if isinstance(hits, list):
            out.extend(h for h in hits if isinstance(h, dict))
    return out


def llm_calls(trace: Any) -> list[dict[str, Any]]:
    """Model name, token usage and latency per model span — what Part 2 must show.

    **Two span types, because there are two paths into a model.** Our own
    `llm_span` (type `LLM`) covers the raw Zen client in `agentlib/core.py`;
    `mlflow.langchain.autolog()` emits type `CHAT_MODEL` for the LangGraph path
    and puts the same three facts under its own attribute names. Reading only one
    type reports half the run's cost and silently omits the agent loop's own
    calls — which are most of them.
    """
    calls: list[dict[str, Any]] = []
    for span in _spans(trace):
        span_type = getattr(span, "span_type", None)
        if span_type not in ("LLM", "CHAT_MODEL"):
            continue
        attributes = getattr(span, "attributes", None) or {}
        start = getattr(span, "start_time_ns", 0) or 0
        end = getattr(span, "end_time_ns", 0) or 0
        cost = attributes.get("radf.cost_usd")
        if cost is None:
            autolog_cost = attributes.get("mlflow.llm.cost")
            if isinstance(autolog_cost, dict):
                cost = autolog_cost.get("total_cost")
        calls.append({
            "name": getattr(span, "name", ""),
            "model": attributes.get("gen_ai.request.model") or attributes.get("mlflow.llm.model"),
            "usage": attributes.get("mlflow.chat.tokenUsage"),
            "cost_usd": cost,
            "latency_ms": round((end - start) / 1e6, 1) if end and start else None,
        })
    return calls


def tool_branches(trace: Any) -> list[dict[str, Any]]:
    """`{tool, branch, gated}` per tool span — the guards' decisions, for 15D.

    Recorded at dispatch rather than re-derived: "the agent re-issued a declined
    `apply_change` four times" is one pass over this list, and is not reliably
    recoverable from arguments alone.
    """
    rows: list[dict[str, Any]] = []
    for span in _spans(trace, "TOOL"):
        attributes = getattr(span, "attributes", None) or {}
        rows.append({
            "tool": attributes.get(TOOL_NAME_ATTR),
            "branch": attributes.get("radf.branch"),
            "gated": attributes.get("radf.gated"),
        })
    return rows


# --- finding traces -----------------------------------------------------------

def get_trace(trace_id: str, *, do_flush: bool = True) -> Optional[Any]:
    """One trace by id. Flushes first — see `tracing.setup.flush`."""
    if not tracing_enabled():
        return None
    if do_flush:
        flush()
    try:
        import mlflow

        return mlflow.get_trace(trace_id)
    except Exception:  # noqa: BLE001
        return None


def find_traces(
    *,
    eval_case_id: Optional[str] = None,
    request_origin: Optional[str] = None,
    run_id: Optional[str] = None,
    extra_filters: Sequence[str] = (),
    limit: int = 100,
) -> list[Any]:
    """Traces matching the ambient tags. The batch entry point for 15C and 15D.

    `eval_case_id` is a join key: pass it to pull back the three runs of one
    scenario. `request_origin` is the filter — `batch` for eval traffic, `ui`
    and `api` for the legitimate traffic a false-positive rate has to be
    measured against.
    """
    if not tracing_enabled():
        return []
    flush()
    clauses = list(extra_filters)
    if eval_case_id:
        clauses.append(f"tags.eval_case_id = '{eval_case_id}'")
    if request_origin:
        clauses.append(f"tags.request_origin = '{request_origin}'")
    if run_id:
        clauses.append(f"tags.`radf.run_id` = '{run_id}'")
    try:
        import mlflow

        return mlflow.search_traces(
            filter_string=" AND ".join(clauses) if clauses else None,
            max_results=limit,
            return_type="list",
        )
    except Exception:  # noqa: BLE001
        return []
