"""tests.test_tracing — the tracing base (HW6, Phase 15A).

Owner: Berat Furkan Kocak.

Two properties matter more than the rest here, and they are the first two tests:

* **Everything no-ops when tracing is off.** HW1-HW5 have to keep running with
  `mlflow` uninstalled and `init_tracing()` never called (decision #90), and that
  is a graded gate, not a nicety. Every helper is exercised in the off state.
* **The tool span is what the trajectory adapter reads.** `gen_ai.tool.name` is a
  string three places have to agree on; a test that only checks "a span exists"
  would pass while `tool_calls_from_trace` returned `[]`.

CLAUDE.md §8 applies — this suite covers framework-touching code, so it carries
an online test at the bottom that makes a real call and reads the real trace back.
The offline body uses hand-built span stubs, because the adapter's contract is
the span *shape*, not MLflow's implementation of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _online import online_key  # noqa: E402,F401  (pytest fixture)
from tracing import spans as spans_mod  # noqa: E402
from tracing import tags as tags_mod  # noqa: E402
from tracing import trajectory as traj  # noqa: E402
from tracing.setup import disable_tracing, init_tracing, tracing_enabled  # noqa: E402


# --- stubs --------------------------------------------------------------------

class _Span:
    """The minimum surface `tracing.trajectory` reads off a span."""

    def __init__(self, name, span_type, start, *, attributes=None, inputs=None,
                 outputs=None, parent_id=None, end=None):
        self.name = name
        self.span_type = span_type
        self.start_time_ns = start
        self.end_time_ns = end if end is not None else start + 1_000_000
        self.attributes = attributes or {}
        self.inputs = inputs
        self.outputs = outputs
        self.parent_id = parent_id


class _Trace:
    def __init__(self, spans):
        self.data = type("D", (), {"spans": spans})()


def _tool_span(name, args, start, *, branch="ok", outputs=None):
    return _Span(f"tool.{name}", "TOOL", start, parent_id="root",
                 attributes={spans_mod.TOOL_NAME_ATTR: name, "radf.branch": branch},
                 inputs=args, outputs=outputs or {"result": "ok"})


@pytest.fixture(autouse=True)
def _tracing_off():
    """Tracing is process-global state; never let one test leak into the next."""
    disable_tracing()
    yield
    disable_tracing()


# --- the off state ------------------------------------------------------------

def test_every_helper_is_a_noop_when_tracing_is_off():
    """The whole point of decision #90: the untraced path must be unchanged."""
    assert not tracing_enabled()
    with spans_mod.agent_span("agent.run", request="hi") as root:
        assert root is None
        with spans_mod.tool_span("evaluate_expression", {"expression": "1+1"}) as tool:
            assert tool is None
            spans_mod.set_outputs(tool, {"result": 2})
            spans_mod.set_attributes(tool, {"radf.branch": "ok"})
        with spans_mod.retriever_span("q", k=5, rerank=True) as r:
            assert r is None
        with spans_mod.llm_span("llm.x", model="m") as llm:
            assert llm is None
            spans_mod.record_usage(llm, {"input_tokens": 1})
    assert spans_mod.current_trace_id() is None


def test_reading_traces_with_tracing_off_returns_empty_not_an_error():
    assert traj.get_trace("tr-nope") is None
    assert traj.find_traces(eval_case_id="ae01") == []


# --- tags ---------------------------------------------------------------------

def test_origin_scope_restores_the_previous_value():
    assert tags_mod.current_origin() is None
    with tags_mod.request_origin_scope("batch"):
        assert tags_mod.current_origin() == "batch"
        with tags_mod.request_origin_scope("ui"):
            assert tags_mod.current_origin() == "ui"
        assert tags_mod.current_origin() == "batch"
    assert tags_mod.current_origin() is None


def test_an_unknown_origin_is_rejected():
    """A typo'd origin defeats the tag's only job silently. Fail loudly instead."""
    with pytest.raises(ValueError):
        with tags_mod.request_origin_scope("cron"):
            pass


def test_trace_tags_drops_empty_values():
    """A tag written as the string "None" would silently join the eval set."""
    with tags_mod.request_origin_scope("batch"):
        out = tags_mod.trace_tags(**{"radf.run_id": None, "radf.agent": "cli"})
    assert out == {"request_origin": "batch", "radf.agent": "cli"}


# --- the trajectory adapter ---------------------------------------------------

def test_tool_calls_are_ordered_by_start_time_not_list_order():
    trace = _Trace([
        _tool_span("query_component_graph", {"component": "a"}, start=300),
        _Span("agent.run", "AGENT", 100, inputs={"request": "q"},
              outputs={"answer": "A", "stopped": "answered"}),
        _tool_span("search_corpus", {"query": "reranker"}, start=200),
    ])
    assert [c.tool for c in traj.tool_calls_from_trace(trace)] == [
        "search_corpus", "query_component_graph",
    ]


def test_trajectory_carries_arguments_and_never_results():
    """Contract #13. Folding results in makes a correct call look wrong the
    moment a tool legitimately errors."""
    trace = _Trace([_tool_span("evaluate_expression", {"expression": "2+2"},
                               start=1, outputs={"result": 4})])
    call = traj.tool_calls_from_trace(trace)[0]
    assert call.arguments == {"expression": "2+2"}
    assert "result" not in call.arguments
    assert "4" not in str(call.arguments)


def test_a_tool_span_without_the_name_attribute_is_skipped():
    """Better to drop a malformed span than to invent a tool named ''."""
    trace = _Trace([_Span("tool.x", "TOOL", 1, inputs={}), ])
    assert traj.tool_calls_from_trace(trace) == []


def test_root_span_accessors():
    root = _Span("agent.run", "AGENT", 1, inputs={"request": "why json?"},
                 outputs={"answer": "because it diffs", "stopped": "answered"})
    trace = _Trace([root, _tool_span("search_corpus", {"query": "json"}, start=2)])
    assert traj.trace_request(trace) == "why json?"
    assert traj.trace_answer(trace) == "because it diffs"
    assert traj.trace_stopped(trace) == "answered"


def test_retrieved_chunks_are_returned_in_retriever_order():
    """Decisions #59/#91: the span records `search()`'s ranking, pre-`pack_for_llm`."""
    hits = [{"chunk_id": "c1", "rank": 1}, {"chunk_id": "c2", "rank": 2},
            {"chunk_id": "c3", "rank": 3}]
    trace = _Trace([_Span("retriever.search", "RETRIEVER", 1,
                          inputs={"query": "q", "k": 3}, outputs={"hits": hits})])
    assert [c["rank"] for c in traj.retrieved_chunks(trace)] == [1, 2, 3]


def test_llm_calls_read_both_our_spans_and_autolog_spans():
    """Two paths reach a model; reading one type reports half the run's cost."""
    trace = _Trace([
        _Span("llm.gpt-5.4-nano", "LLM", 1, end=1 + 2_000_000, attributes={
            "gen_ai.request.model": "gpt-5.4-nano", "radf.cost_usd": 0.001,
            "mlflow.chat.tokenUsage": {"input_tokens": 10, "output_tokens": 2},
        }),
        _Span("ChatOpenAI", "CHAT_MODEL", 5, attributes={
            "mlflow.llm.model": "gpt-5.4-nano",
            "mlflow.chat.tokenUsage": {"input_tokens": 100, "output_tokens": 20},
            "mlflow.llm.cost": {"total_cost": 0.002},
        }),
    ])
    calls = traj.llm_calls(trace)
    assert [c["model"] for c in calls] == ["gpt-5.4-nano", "gpt-5.4-nano"]
    assert [c["cost_usd"] for c in calls] == [0.001, 0.002]
    assert calls[0]["latency_ms"] == 2.0


def test_tool_branches_expose_the_guards_decision():
    """15D reads this instead of re-deriving intent from arguments (contract #12)."""
    trace = _Trace([
        _tool_span("prune_graph_node", {"node_id": "x"}, start=1, branch="declined"),
        _tool_span("prune_graph_node", {"node_id": "x"}, start=2, branch="declined"),
    ])
    assert [row["branch"] for row in traj.tool_branches(trace)] == ["declined", "declined"]


# --- online (CLAUDE.md §8) ----------------------------------------------------

@pytest.mark.online
def test_online_a_real_run_produces_a_readable_span_tree(online_key, tmp_path, monkeypatch):
    """One real traced run: root span, tool span, tags, and the trajectory back out.

    This is the test the offline body cannot stand in for. A mocked model never
    has to satisfy LangGraph's calling contract, so a tool span opened at the
    wrong place, a tag that never reaches the trace, or an adapter reading an
    attribute MLflow does not actually write would all pass offline and fail here.
    """
    monkeypatch.setenv("RADF_MLFLOW_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    assert init_tracing("radf-test"), "mlflow must be installed for the online test"

    from agentlib.core import CHEAP
    from agentlib.loop import run_agent
    from agentlib.runlog import RunLog
    from agentlib.schemas import schema_for
    from tools.utility_tools import evaluate_expression
    from tracing import eval_case_scope, request_origin_scope

    run_log = RunLog(agent="test", user_id="tester", request="")
    with request_origin_scope("batch"), eval_case_scope("t01"):
        result = run_agent(
            "What is 617 * 3? Use your tools.",
            [schema_for(evaluate_expression)],
            {"evaluate_expression": evaluate_expression},
            model=CHEAP, max_steps=4, verbose=False, run_log=run_log,
        )

    assert result["stopped"] == "answered"
    assert run_log.trace_id, "the run must record its trace id for the runs.jsonl join"

    trace = traj.get_trace(run_log.trace_id)
    assert trace is not None, "flush() should make the trace readable immediately"

    types = {s.span_type for s in trace.data.spans}
    assert "AGENT" in types and "TOOL" in types

    calls = traj.tool_calls_from_trace(trace)
    assert [c.tool for c in calls] == ["evaluate_expression"]
    assert "617" in str(calls[0].arguments)

    assert trace.info.tags["request_origin"] == "batch"
    assert trace.info.tags["eval_case_id"] == "t01"
    assert trace.info.tags["radf.run_id"] == run_log.run_id
    assert traj.trace_stopped(trace) == "answered"

    # Model name and token counts must be visible on a model span (Part 2).
    usage = [c for c in traj.llm_calls(trace) if c["usage"]]
    assert usage and usage[0]["model"]
