"""tests/test_loop_contract.py — `run_agent`'s frozen contract, after the refactor.

Owner: Dias Sarkytbaev (HW4, Phase 13b). New suite (CLAUDE.md §8).

Phase 12 replaced `run_agent`'s internals with a compiled LangGraph graph and
froze its *interface* as contract #9: the call signature every downstream caller
uses, and the return shape `{"answer", "steps", "trace", "stopped"}` (+ `run_id`
when a run log is passed), with every trace entry tagged
`ok | error | declined | invalid_args`.

That freeze is what makes Phase 13b a verification pass rather than a rewrite —
`agents/executor.py`, `agents/admin.py`, `service.py` and `main.py` all import
`run_agent` and none of them changed. This suite is the executable statement of
that claim: if a future refactor drifts the shape, four callers break at once and
the failure should surface here, on the contract, rather than as four unrelated
bugs downstream.

Deliberately NOT a copy of `test_graph_agent.py`. That suite tests the graph's
internals (state schema, tool wrapping, routing); this one tests only what
crosses the boundary, which is the part other people's code depends on.

Run:  python -m pytest tests/test_loop_contract.py -v
"""

from __future__ import annotations

import inspect

import pytest
from _online import online_key  # noqa: F401 — pytest fixture, used by name
from langchain_core.messages import AIMessage

import agentlib.graph as graph_module
from agentlib.loop import run_agent
from agentlib.runlog import RunLog
from agentlib.schemas import schema_for

#: Every keyword the four in-repo callers pass, gathered from their call sites:
#: agents/executor.py:194, agents/admin.py:160, service.py:187, main.py:98.
#: Dropping any one of these silently breaks that caller.
CALLER_KWARGS = frozenset(
    {"approve", "model", "max_steps", "verbose", "context", "run_log"}
)

#: Every key those callers read back off the result.
CALLER_READS = frozenset({"answer", "stopped", "trace", "steps", "run_id"})

#: The keys the contract guarantees on every return, run log or not.
FROZEN_KEYS = frozenset({"answer", "steps", "trace", "stopped"})


# --- helpers: the offline scripted model (same seam as test_graph_agent.py) ---


def echo_tool(value: str) -> dict:
    """A trivial reversible tool. Returns what it was given."""
    return {"echoed": value}


def boom_tool(value: str) -> dict:
    """A tool that always returns a structured error, for the error branch."""
    return {"error": "boom", "value": value}


def prune_graph_node(node_id: str) -> dict:
    """Named to match `guards.GATED` — only the NAME decides gating."""
    return {"pruned": node_id}


def _tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class _ScriptedModel:
    """Replays canned `AIMessage`s in place of the bound chat model.

    Pops strictly: an under-scripted test fails loudly on IndexError rather than
    quietly repeating a reply and testing something else than it says it does.
    """

    def __init__(self, replies: list[AIMessage]):
        self._replies = list(replies)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return self._replies.pop(0)


class _LoopingModel:
    """Asks for the same tool forever, with a DIFFERENT argument each turn.

    Two subtleties this class exists to get right, both learned the hard way:

    * **Distinct arguments.** Identical ones trip stall detection, which stops
      the run before the step cap is ever reached — so a "max_steps" test built
      on a repeated call is really a stall test wearing the wrong name.
    * **A fresh `AIMessage` per turn.** `add_messages` dedupes by message id, so
      returning the *same* object twice silently drops the second one; the graph
      then re-enters `tools_node` with a ToolMessage last and trips its
      `isinstance(last, AIMessage)` assertion. The bug looks like a graph bug and
      is a test bug.
    """

    def __init__(self, tool_name: str):
        self._tool = tool_name
        self._turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self._turn += 1
        return AIMessage(content="", tool_calls=[
            _tool_call(self._tool, {"value": f"v{self._turn}"}, f"c{self._turn}")])


def _patch_model(monkeypatch, replies: list[AIMessage]) -> None:
    monkeypatch.setattr(graph_module, "_build_chat_model",
                        lambda model: _ScriptedModel(replies))


def _run(monkeypatch, replies, *, registry=None, **kwargs):
    registry = registry or {"echo_tool": echo_tool}
    _patch_model(monkeypatch, replies)
    return run_agent(
        "do the thing",
        [schema_for(fn) for fn in registry.values()],
        registry,
        verbose=False,
        **kwargs,
    )


# --- the call signature four other files depend on ----------------------------


class TestSignatureContract:
    def test_run_agent_still_accepts_every_caller_keyword(self):
        """executor/admin/service/main pass these by name — all six must remain."""
        params = set(inspect.signature(run_agent).parameters)
        missing = CALLER_KWARGS - params
        assert not missing, f"contract #9 drift: run_agent lost {sorted(missing)}"

    def test_the_first_three_arguments_are_still_positional(self):
        """All four callers pass user_msg/schemas/registry positionally."""
        params = list(inspect.signature(run_agent).parameters)
        assert params[:3] == ["user_msg", "schemas", "registry"]


# --- the return shape, per stopping condition ---------------------------------


class TestReturnShape:
    def test_answered_returns_exactly_the_frozen_keys(self, monkeypatch):
        result = _run(monkeypatch, [AIMessage(content="done")])
        assert set(result) == FROZEN_KEYS
        assert result["stopped"] == "answered"
        assert result["answer"] == "done"
        assert isinstance(result["steps"], int)
        assert isinstance(result["trace"], list)

    def test_a_tool_run_tags_its_trace_entry_ok(self, monkeypatch):
        result = _run(monkeypatch, [
            AIMessage(content="", tool_calls=[_tool_call("echo_tool", {"value": "hi"}, "c1")]),
            AIMessage(content="said hi"),
        ])
        assert result["stopped"] == "answered"
        assert [e["branch"] for e in result["trace"]] == ["ok"]
        assert set(result["trace"][0]) >= {"tool", "args", "output", "branch"}

    def test_a_structured_error_tags_its_trace_entry_error(self, monkeypatch):
        result = _run(monkeypatch, [
            AIMessage(content="", tool_calls=[_tool_call("boom_tool", {"value": "x"}, "c1")]),
            AIMessage(content="it failed"),
        ], registry={"boom_tool": boom_tool})
        assert [e["branch"] for e in result["trace"]] == ["error"]

    def test_bad_args_tag_their_trace_entry_invalid_args(self, monkeypatch):
        result = _run(monkeypatch, [
            AIMessage(content="", tool_calls=[_tool_call("echo_tool", {"wrong": 1}, "c1")]),
            AIMessage(content="recovered"),
        ])
        assert [e["branch"] for e in result["trace"]] == ["invalid_args"]

    def test_a_declined_gate_tags_its_trace_entry_declined(self, monkeypatch):
        result = _run(monkeypatch, [
            AIMessage(content="", tool_calls=[
                _tool_call("prune_graph_node", {"node_id": "Module:x"}, "c1")]),
            AIMessage(content="understood"),
        ], registry={"prune_graph_node": prune_graph_node},
            approve=lambda name, args: False)
        assert [e["branch"] for e in result["trace"]] == ["declined"]

    def test_max_steps_still_stops_and_keeps_the_shape(self, monkeypatch):
        """A model that never stops asking; the cap is the CODE's decision."""
        monkeypatch.setattr(graph_module, "_build_chat_model",
                            lambda model: _LoopingModel("echo_tool"))
        result = run_agent("do the thing", [schema_for(echo_tool)],
                           {"echo_tool": echo_tool}, max_steps=2, verbose=False)

        assert result["stopped"] == "max_steps"
        assert result["steps"] == 2
        assert set(result) == FROZEN_KEYS

    def test_a_repeated_identical_call_stops_as_stalled(self, monkeypatch):
        """The other loop-owned stop: same tool, same args, twice."""
        same = {"value": "again"}
        result = _run(monkeypatch, [
            AIMessage(content="", tool_calls=[_tool_call("echo_tool", same, "c1")]),
            AIMessage(content="", tool_calls=[_tool_call("echo_tool", same, "c2")]),
        ], max_steps=8)
        assert result["stopped"] == "stalled"
        assert set(result) == FROZEN_KEYS

    def test_every_stopping_condition_is_one_of_the_named_five(self, monkeypatch):
        result = _run(monkeypatch, [AIMessage(content="done")])
        assert result["stopped"] in (
            "answered", "max_steps", "stalled", "declined", "truncated")


# --- the run log seam ---------------------------------------------------------


class TestRunLogContract:
    def test_run_id_is_added_only_when_a_run_log_is_passed(self, monkeypatch):
        """service.py reads result["run_id"]; executor.py passes run_log=None."""
        without = _run(monkeypatch, [AIMessage(content="done")])
        assert "run_id" not in without

        log = RunLog(agent="test", user_id="dias", thread_id="t1")
        with_log = _run(monkeypatch, [AIMessage(content="done")], run_log=log)
        assert with_log["run_id"] == log.run_id

    def test_the_run_is_flushed_so_the_monitor_can_grade_it(self, monkeypatch):
        """My monitor reads runs.jsonl — a run that never lands is never graded."""
        from agentlib.runlog import read_runs

        log = RunLog(agent="test", user_id="dias", thread_id="t1")
        result = _run(monkeypatch, [AIMessage(content="done")], run_log=log)
        landed = [r for r in read_runs() if r["run_id"] == result["run_id"]]
        assert landed, "run_agent did not flush the run log"
        assert landed[0]["stopped"] == "answered"

    def test_the_keys_callers_read_are_all_available(self, monkeypatch):
        log = RunLog(agent="test", user_id="dias", thread_id="t1")
        result = _run(monkeypatch, [AIMessage(content="done")], run_log=log)
        missing = CALLER_READS - set(result)
        assert not missing, f"callers read {sorted(missing)}, which the result lacks"


# --- the downstream callers, unchanged ----------------------------------------


class TestDownstreamCallersStillWork:
    def test_the_four_callers_import_run_agent_without_edits(self):
        """A stale import would be the loudest possible contract drift."""
        import agents.admin as admin_mod
        import agents.executor as executor_mod
        import main as main_mod
        import service as service_mod

        for module in (executor_mod, admin_mod, service_mod, main_mod):
            assert getattr(module, "run_agent", None) is run_agent, (
                f"{module.__name__} does not import the frozen run_agent")

    def test_run_admin_drives_the_refactored_loop_end_to_end(self, monkeypatch):
        """admin.py needed no change: it still gets an `ok` envelope back."""
        from agentlib.session import SessionKey
        from agents.admin import run_admin
        from channel.identity import Identity

        _patch_model(monkeypatch, [AIMessage(content="nothing to change")])
        identity = Identity(session=SessionKey(user_id="berat"), known=True,
                            is_admin=True, external_id="1", source="telegram")

        out = run_admin("summarise the decisions on tools.decisions",
                        identity=identity, confirmed=True, verbose=False)

        assert out.status == "ok"
        assert out.result["answer"] == "nothing to change"


# --- online: the same contract, against a real model --------------------------


@pytest.mark.online
def test_online_a_real_run_satisfies_the_same_frozen_shape(online_key):
    """One live call — the offline tests all script the model, so none of them
    prove the shape survives a real provider round trip through the graph.

    Asserted: the frozen keys, a named stopping condition, and a `run_id` once a
    run log is attached. Not asserted: the answer's wording."""
    from agentlib.core import CHEAP

    log = RunLog(agent="contract-online", user_id="dias", thread_id="t1")
    result = run_agent(
        "Reply with the single word: ok",
        schemas=[],
        registry={},
        model=CHEAP,
        verbose=False,
        run_log=log,
    )

    assert FROZEN_KEYS <= set(result)
    assert result["stopped"] in (
        "answered", "max_steps", "stalled", "declined", "truncated")
    assert result["run_id"] == log.run_id
    assert isinstance(result["trace"], list)
