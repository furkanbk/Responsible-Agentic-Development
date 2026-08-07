"""tracing.spans — the four span helpers the instrumentation sites call (T15.2).

Owner: Berat Furkan Kocak. Contract **#12** (docs/TODO.md, HW6).

Four span types, because Part 2 asks for exactly four things in the tree:

    agent_span      root, one per agent invocation
    llm_span        model name, token counts, cost; latency is the span itself
    tool_span       one per dispatched tool call, with the guard's branch
    retriever_span  the search, in RETRIEVER order

`mlflow.langchain.autolog()` produces the LangGraph path's LLM spans, and nothing
else we need: the tools node in `agentlib/graph.py` is hand-written rather than
LangChain's `ToolNode`, so there are no `TOOL` spans to autolog and the trajectory
adapter would have nothing to read (decision #88). Everything else here is
instrumented by hand for that reason.

**Two rules every helper follows.**

1. *It yields `None` when tracing is off*, so `with tool_span(...) as sp:` is
   always safe to write and the untraced path stays identical (decision #90).
2. *It never raises.* A span that fails to open, or an attribute that fails to
   serialise, costs observability. Taking the agent down with it would cost the
   run. Exceptions from the wrapped BODY still propagate — and are recorded on
   the span before they do, which is the whole point of tracing an error.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .setup import tracing_enabled
from .tags import trace_tags

# The attribute the trajectory adapter reads a tool's name from. Session 12's
# convention, and the reason it is a constant: `tool_calls_from_trace` and every
# instrumentation site must agree on one string, or the adapter returns [].
TOOL_NAME_ATTR = "gen_ai.tool.name"

# The guard's decision, recorded where a scorer can read it. Already computed at
# the dispatch site by `agentlib/guards.py`, so putting it on the span costs
# nothing and saves the safety detector from re-deriving intent from arguments.
BRANCH_ATTR = "radf.branch"
GATED_ATTR = "radf.gated"

MODEL_ATTR = "gen_ai.request.model"
USAGE_ATTR = "mlflow.chat.tokenUsage"
COST_ATTR = "radf.cost_usd"


def _safe(fn, *args, **kwargs) -> None:
    """Run a span mutation, swallowing anything it raises. See rule 2 above."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def set_outputs(span: Optional[Any], value: Any) -> None:
    """Set a span's outputs if there is a span. The no-op path in one place."""
    if span is None:
        return
    _safe(span.set_outputs, value)


def set_attributes(span: Optional[Any], attributes: dict[str, Any]) -> None:
    """Set several attributes if there is a span."""
    if span is None:
        return
    for key, val in attributes.items():
        if val is None:
            continue
        _safe(span.set_attribute, key, val)


def current_trace_id() -> Optional[str]:
    """The active trace id, or None. This is the join key into `runs.jsonl`."""
    if not tracing_enabled():
        return None
    try:
        import mlflow

        return mlflow.get_current_active_span().trace_id  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return None


@contextmanager
def _span(
    name: str,
    span_type: str,
    *,
    inputs: Optional[Any] = None,
    attributes: Optional[dict[str, Any]] = None,
    tags: Optional[dict[str, str]] = None,
) -> Iterator[Optional[Any]]:
    """The one place a span is opened and closed. Everything else wraps this."""
    if not tracing_enabled():
        yield None
        return

    try:
        import mlflow

        manager = mlflow.start_span(name=name, span_type=span_type)
        span = manager.__enter__()
    except Exception:  # noqa: BLE001
        yield None
        return

    if inputs is not None:
        _safe(span.set_inputs, inputs)
    set_attributes(span, attributes or {})
    if tags:
        try:
            import mlflow as _mlflow

            _mlflow.update_current_trace(tags=tags)
        except Exception:  # noqa: BLE001
            pass

    try:
        yield span
    except BaseException as exc:
        # Close the span WITH the exception so the trace shows the failure
        # rather than an unexplained truncation, then let it propagate. A run
        # that died is exactly the run a judge most wants to read.
        try:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def agent_span(
    name: str = "agent.run",
    *,
    request: str = "",
    run_id: Optional[str] = None,
    agent: Optional[str] = None,
    user_id: Optional[str] = None,
    tags: Optional[dict[str, str]] = None,
) -> Iterator[Optional[Any]]:
    """The root span for one agent invocation. Tags the trace for later search.

    `run_id` is the `RunLog` id, written as `radf.run_id`. That tag is the join
    into `store/runs/runs.jsonl`, which holds the one thing a span tree does not:
    what was ASSEMBLED into the model's context. An adapter that needs the
    instructions (the monitor's, T15.10) reads them through this tag.

    `request_origin` and `eval_case_id` come from the ambient scope, never from
    an argument here — see `tracing/tags.py`.
    """
    extra: dict[str, Any] = {
        "radf.run_id": run_id, "radf.agent": agent, "radf.user_id": user_id,
    }
    extra.update(tags or {})   # caller's tags win; building the dict first
    merged = trace_tags(**extra)   # means a duplicate key overrides, not raises
    with _span(
        name,
        _span_type("AGENT"),
        inputs={"request": request},
        attributes={"radf.run_id": run_id, "radf.agent": agent},
        tags=merged,
    ) as span:
        yield span


@contextmanager
def tool_span(tool_name: str, args: Optional[dict[str, Any]] = None) -> Iterator[Optional[Any]]:
    """One dispatched tool call. **The load-bearing span of HW6.**

    The trajectory adapter reads `gen_ai.tool.name` and these inputs; the safety
    detector reads `radf.branch`, set by the caller once the guards have decided.
    Outputs are recorded too — a trajectory ignores them, but indirect-injection
    detection cannot work without seeing what came back.
    """
    with _span(
        f"tool.{tool_name}",
        _span_type("TOOL"),
        inputs=dict(args or {}),
        attributes={TOOL_NAME_ATTR: tool_name},
    ) as span:
        yield span


@contextmanager
def llm_span(
    name: str,
    *,
    model: str,
    inputs: Optional[Any] = None,
) -> Iterator[Optional[Any]]:
    """One model round trip. Token counts and cost are set by the caller on exit,
    because only the caller has the response. Latency is the span's own duration."""
    with _span(
        name,
        _span_type("LLM"),
        inputs=inputs,
        attributes={MODEL_ATTR: model},
    ) as span:
        yield span


def record_usage(span: Optional[Any], usage: dict[str, int], cost: Optional[float] = None) -> None:
    """Put token counts (and our cost estimate) on an LLM span, in MLflow's shape."""
    if span is None or not usage:
        return
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    set_attributes(span, {
        USAGE_ATTR: {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "gen_ai.usage.cached_input_tokens": int(usage.get("cached_input_tokens", 0) or 0),
        "gen_ai.usage.reasoning_tokens": int(usage.get("reasoning_tokens", 0) or 0),
        COST_ATTR: cost,
    })


@contextmanager
def retriever_span(
    query: str,
    *,
    k: int,
    rerank: bool,
    source: str = "all",
) -> Iterator[Optional[Any]]:
    """One `search()` call.

    The caller records the hits in **retriever order**, before `pack_for_llm`
    reorders them for the context window (decisions #59, #91). Feeding a repacked
    list to MRR and nDCG degrades two of the five retrieval metrics while the
    other three look fine, which is the kind of bug that is only ever found after
    the numbers are in a report.
    """
    with _span(
        "retriever.search",
        _span_type("RETRIEVER"),
        inputs={"query": query, "k": k, "rerank": rerank, "source": source},
    ) as span:
        yield span


def _span_type(name: str) -> str:
    """`SpanType.X` if mlflow is importable, else the plain string.

    The enum's values ARE these strings; resolving through the enum keeps us
    honest if MLflow renames one, and the fallback keeps this module importable
    with mlflow absent (decision #90).
    """
    try:
        from mlflow.entities import SpanType

        return getattr(SpanType, name)
    except Exception:  # noqa: BLE001
        return name
