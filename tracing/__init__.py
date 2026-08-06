"""tracing — MLflow tracing for the agent (HW6, Phase 15A).

Owner: Berat Furkan Kocak (T15.2).

What this package is for: a run leaves behind a **span tree** — one root per agent
invocation, children for LLM calls, tool executions and retrieval — that the eval
harness, the RAG scorers and the safety detector can all read afterwards, without
re-running anything.

What it is NOT: a replacement for `agentlib/runlog.py`. `store/runs/runs.jsonl`
stays the durable record, and it carries one thing no span tree does — what was
ASSEMBLED (the instructions and the pulled source ids), which is how "the agent
ignored a rule" is told apart from "the rule was never in its context". The
MLflow store is derived and may be dropped and rebuilt at any time (decision #89,
the same argument decision #62 makes for pgvector). The two are **joined, never
merged**: a `radf.run_id` tag on the trace, `trace_id` in `RunLog.scratch`.

Tracing is opt-in per process and every helper degrades to a no-op when
`init_tracing()` was never called or `mlflow` is not installed (decision #90).
The rest of the system must keep working untraced.

    from tracing import init_tracing, request_origin_scope
    init_tracing()
    with request_origin_scope("batch"):
        run_agent(...)
"""

from __future__ import annotations

from .setup import (
    DEFAULT_EXPERIMENT,
    disable_tracing,
    flush,
    init_tracing,
    tracing_enabled,
    tracking_uri,
)
from .spans import (
    BRANCH_ATTR,
    GATED_ATTR,
    TOOL_NAME_ATTR,
    agent_span,
    current_trace_id,
    llm_span,
    retriever_span,
    set_outputs,
    tool_span,
)
from .tags import (
    ORIGINS,
    current_eval_case_id,
    current_origin,
    eval_case_scope,
    request_origin_scope,
    trace_tags,
)
from .trajectory import (
    ToolCall,
    find_traces,
    get_trace,
    llm_calls,
    retrieved_chunks,
    root_span,
    tool_branches,
    tool_calls_from_trace,
    trace_answer,
    trace_request,
    trace_stopped,
)

__all__ = [
    # setup
    "DEFAULT_EXPERIMENT", "init_tracing", "tracing_enabled", "flush",
    "disable_tracing", "tracking_uri",
    # tags
    "ORIGINS", "request_origin_scope", "eval_case_scope", "current_origin",
    "current_eval_case_id", "trace_tags",
    # spans
    "agent_span", "tool_span", "llm_span", "retriever_span", "set_outputs",
    "current_trace_id", "TOOL_NAME_ATTR", "BRANCH_ATTR", "GATED_ATTR",
    # trajectory
    "ToolCall", "tool_calls_from_trace", "trace_request", "trace_answer",
    "trace_stopped", "retrieved_chunks", "llm_calls", "tool_branches",
    "root_span", "find_traces", "get_trace",
]
