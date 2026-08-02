"""tests.test_graph_agent — HW4 Phase 12: the LangGraph-backed agentlib.loop.

Owner: Berat Furkan Kocak. New suite (CLAUDE.md §8) — the pre-refactor suites
stay untouched and keep validating the pre-refactor contract of `run_agent`.
One test is marked `online` (a real call through the compiled graph, against
the real Zen endpoint) and is skipped when `OPENCODE_API_KEY` is unset; the
rest are offline, driven by a scripted fake chat model so they run without
network access or cost.

Framework conversion bugs (a tool schema that never reaches the model, a state
field the graph drops, a routing edge that never fires) are exactly what a
mocked-everything suite cannot catch (CLAUDE.md §8) — the online test is what
proves `bind_tools` + the Zen Responses endpoint actually agree on a tool call.
"""

from __future__ import annotations

import os
import typing

import pytest
from langchain_core.messages import AIMessage

import agentlib.graph as graph_module
from agentlib.graph_state import AgentState
from agentlib.langchain_tools import build_langchain_tools, to_langchain_tool
from agentlib.loop import run_agent
from agentlib.schemas import schema_for


# --- helpers ------------------------------------------------------------------

def evaluate_expression(expression: str) -> dict:
    """A copy of tools.utility_tools.evaluate_expression's signature for tests."""
    return {"expression": expression, "result": eval(expression, {"__builtins__": {}})}


def prune_graph_node(node_id: str) -> dict:
    """Stands in for the real gated tool — only the NAME matters to guards.GATED."""
    return {"pruned": node_id}


def boom_tool(x: int) -> dict:
    """A tool that always returns a structured error."""
    return {"error": "boom", "x": x}


def _tc(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class _ScriptedModel:
    """Stands in for `_build_chat_model(...).bind_tools(...)`: replays canned replies."""

    def __init__(self, replies: list[AIMessage]):
        self._replies = list(replies)
        self.invocations: list[list] = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.invocations.append(list(messages))
        return self._replies.pop(0)


def _patch_model(monkeypatch, replies: list[AIMessage]) -> _ScriptedModel:
    scripted = _ScriptedModel(replies)
    monkeypatch.setattr(graph_module, "_build_chat_model", lambda model: scripted)
    return scripted


# --- state schema ---------------------------------------------------------

def test_agent_state_is_explicit_typed_dict():
    """Not an ad-hoc dict — the professor's stated requirement (decision #51)."""
    hints = typing.get_type_hints(AgentState, include_extras=True)
    assert set(hints) == {
        "messages", "trace", "signatures", "declined_signatures", "step", "stopped", "answer",
    }


# --- tool-wrapping convention (decision #50) -------------------------------

def test_to_langchain_tool_preserves_name_and_description():
    lc_tool = to_langchain_tool(evaluate_expression)
    assert lc_tool.name == "evaluate_expression"
    assert lc_tool.description.startswith("A copy of")


def test_build_langchain_tools_preserves_order():
    fns = [evaluate_expression, prune_graph_node, boom_tool]
    lc_tools = build_langchain_tools(fns)
    assert [t.name for t in lc_tools] == [fn.__name__ for fn in fns]


# --- offline graph behavior, via run_agent (the frozen public contract) ---

def test_answered_no_tool_call(monkeypatch):
    _patch_model(monkeypatch, [AIMessage(content="the answer is 42")])
    result = run_agent(
        "what is the answer?",
        schemas=[schema_for(evaluate_expression)],
        registry={"evaluate_expression": evaluate_expression},
        verbose=False,
    )
    assert result == {
        "answer": "the answer is 42", "steps": 0, "trace": [], "stopped": "answered",
    }


def test_run_agent_return_shape_unchanged(monkeypatch):
    """Pins the frozen contract (#9): same keys as the pre-refactor loop, always."""
    _patch_model(monkeypatch, [AIMessage(content="ok")])
    result = run_agent(
        "hi", schemas=[], registry={}, verbose=False,
    )
    assert set(result) == {"answer", "steps", "trace", "stopped"}


def test_tool_call_then_answer(monkeypatch):
    tc = _tc("evaluate_expression", {"expression": "2 + 2"}, "call_1")
    _patch_model(monkeypatch, [
        AIMessage(content="", tool_calls=[tc]),
        AIMessage(content="the result is 4"),
    ])
    result = run_agent(
        "what is 2 + 2?",
        schemas=[schema_for(evaluate_expression)],
        registry={"evaluate_expression": evaluate_expression},
        verbose=False,
    )
    assert result["stopped"] == "answered"
    assert result["answer"] == "the result is 4"
    assert result["steps"] == 1
    assert result["trace"] == [{
        "tool": "evaluate_expression",
        "args": {"expression": "2 + 2"},
        "output": {"expression": "2 + 2", "result": 4},
        "branch": "ok",
    }]


def test_invalid_args_branch(monkeypatch):
    tc = _tc("evaluate_expression", {}, "call_1")  # missing required 'expression'
    _patch_model(monkeypatch, [
        AIMessage(content="", tool_calls=[tc]),
        AIMessage(content="fixed"),
    ])
    result = run_agent(
        "compute something",
        schemas=[schema_for(evaluate_expression)],
        registry={"evaluate_expression": evaluate_expression},
        verbose=False,
    )
    assert result["trace"][0]["branch"] == "invalid_args"
    assert result["trace"][0]["output"]["error"] == "invalid_args"


def test_error_branch(monkeypatch):
    tc = _tc("boom_tool", {"x": 1}, "call_1")
    _patch_model(monkeypatch, [
        AIMessage(content="", tool_calls=[tc]),
        AIMessage(content="handled"),
    ])
    result = run_agent(
        "trigger boom",
        schemas=[schema_for(boom_tool)],
        registry={"boom_tool": boom_tool},
        verbose=False,
    )
    assert result["trace"][0]["branch"] == "error"


def test_gate_decline_then_reissue_stops_declined(monkeypatch):
    """A gated tool declined, then re-issued -> stopped='declined' (decision #10)."""
    tc = _tc("prune_graph_node", {"node_id": "n1"}, "call_1")
    same_tc = _tc("prune_graph_node", {"node_id": "n1"}, "call_2")
    _patch_model(monkeypatch, [
        AIMessage(content="", tool_calls=[tc]),
        AIMessage(content="", tool_calls=[same_tc]),
    ])
    result = run_agent(
        "prune node n1",
        schemas=[schema_for(prune_graph_node)],
        registry={"prune_graph_node": prune_graph_node},
        approve=lambda name, args: False,
        verbose=False,
    )
    assert result["trace"][0]["branch"] == "declined"
    assert result["stopped"] == "declined"


def test_gate_approved_runs_tool(monkeypatch):
    tc = _tc("prune_graph_node", {"node_id": "n1"}, "call_1")
    _patch_model(monkeypatch, [
        AIMessage(content="", tool_calls=[tc]),
        AIMessage(content="pruned"),
    ])
    result = run_agent(
        "prune node n1",
        schemas=[schema_for(prune_graph_node)],
        registry={"prune_graph_node": prune_graph_node},
        approve=lambda name, args: True,
        verbose=False,
    )
    assert result["trace"][0] == {
        "tool": "prune_graph_node", "args": {"node_id": "n1"},
        "output": {"pruned": "n1"}, "branch": "ok",
    }
    assert result["stopped"] == "answered"


def test_stall_detected_within_one_batch(monkeypatch):
    """Two identical calls in one model turn -> the second is a stall (guards.detect_stall)."""
    tc = _tc("evaluate_expression", {"expression": "1 + 1"}, "call_1")
    tc_repeat = _tc("evaluate_expression", {"expression": "1 + 1"}, "call_2")
    _patch_model(monkeypatch, [AIMessage(content="", tool_calls=[tc, tc_repeat])])
    result = run_agent(
        "compute 1+1 twice",
        schemas=[schema_for(evaluate_expression)],
        registry={"evaluate_expression": evaluate_expression},
        verbose=False,
    )
    assert result["stopped"] == "stalled"
    assert len(result["trace"]) == 1  # the stalled call itself never executed


def test_max_steps(monkeypatch):
    tc_for = lambda i: _tc("evaluate_expression", {"expression": f"{i} + 1"}, f"call_{i}")
    replies = [AIMessage(content="", tool_calls=[tc_for(i)]) for i in range(3)]
    _patch_model(monkeypatch, replies)
    result = run_agent(
        "keep computing",
        schemas=[schema_for(evaluate_expression)],
        registry={"evaluate_expression": evaluate_expression},
        max_steps=3,
        verbose=False,
    )
    assert result["stopped"] == "max_steps"
    assert result["steps"] == 3


# --- online: a real call through the compiled graph ------------------------

@pytest.mark.online
@pytest.mark.skipif(not os.environ.get("OPENCODE_API_KEY"), reason="OPENCODE_API_KEY not set")
def test_online_agent_calls_calculator_tool():
    from agentlib.core import CHEAP

    result = run_agent(
        "What is 19 * 3? Use the evaluate_expression tool, then answer with just the number.",
        schemas=[schema_for(evaluate_expression)],
        registry={"evaluate_expression": evaluate_expression},
        model=CHEAP,
        verbose=False,
    )
    assert result["stopped"] == "answered"
    assert any(step["tool"] == "evaluate_expression" for step in result["trace"])
    assert "57" in (result["answer"] or "")
