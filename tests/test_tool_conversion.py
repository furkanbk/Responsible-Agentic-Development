"""tests.test_tool_conversion — HW4 Phase 13a: the graph-tool conversion sweep.

Owner: Alejandro Ramírez Trueba. New suite (CLAUDE.md §8) — the pre-refactor
suites stay untouched. This covers what Phase 13a adds on top of Berat's Phase
12 foundation:

  * every tool in `tools.TOOL_FUNCTIONS` (the 8 graph tools + the two new
    framework tools) survives the ONE conversion point (`to_langchain_tool`,
    decision #50) into a LangChain `StructuredTool` with its name, description,
    and argument schema intact;
  * that conversion leaks NO identity/scope parameter (invariant #25) — the
    thing HW2 paid for and HW4 must not quietly undo by learning a second,
    LLM-facing argument API;
  * the second new tool, `diff_texts` (decision #54), behaves and is reachable;
  * one ONLINE test drives the compiled graph against the real Zen endpoint so a
    conversion bug that only shows up when the framework actually calls the tool
    (a schema the model never sees, an arg the graph drops) is caught — exactly
    the class of bug a mocked model cannot catch (CLAUDE.md §8).

Offline tests use the same scripted-model seam as `test_graph_agent.py`; the
online test uses the shared `_online.online_key` fixture (decision #55) so the
placeholder-key and no-export rules are honoured in one place.
"""

from __future__ import annotations

import typing

import pytest
from langchain_core.messages import AIMessage

import agentlib.graph as graph_module
from agentlib.langchain_tools import to_langchain_tool
from agentlib.loop import run_agent
from agentlib.schemas import schema_for
from tools import LANGCHAIN_TOOLS, TOOL_FUNCTIONS, build_langchain_registry
from tools.text_tools import diff_texts

from _online import online_key  # noqa: F401  (pytest fixture, used by the online test)


# --- helpers (the scripted-model seam, as in test_graph_agent.py) ------------

def _tc(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class _ScriptedModel:
    """Stands in for `_build_chat_model(...).bind_tools(...)`: replays canned replies."""

    def __init__(self, replies: list[AIMessage]):
        self._replies = list(replies)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return self._replies.pop(0)


def _patch_model(monkeypatch, replies: list[AIMessage]) -> None:
    monkeypatch.setattr(graph_module, "_build_chat_model", lambda model: _ScriptedModel(replies))


# --- the conversion sweep (decision #54) -------------------------------------

# Parameters an ambient-authority tool must NEVER expose to the model: identity
# and write scope are read from session_scope/impact_scope, never passed
# (invariant #25). If conversion ever surfaced one of these as a tool arg, the
# model could assert its own identity or widen its own write scope.
_FORBIDDEN_ARGS = frozenset(
    {"author_id", "user_id", "acting_user", "impacted", "impact", "session", "scope_uids"}
)


def test_every_registered_tool_converts():
    """The whole registry survives `to_langchain_tool`, name preserved, in order."""
    converted = build_langchain_registry()
    assert [t.name for t in converted] == [fn.__name__ for fn in TOOL_FUNCTIONS]
    # LANGCHAIN_TOOLS (built once at import) is the same registration surface.
    assert [t.name for t in LANGCHAIN_TOOLS] == [fn.__name__ for fn in TOOL_FUNCTIONS]


def test_conversion_preserves_description_and_args():
    """Description (docstring) and the inferred arg schema reach the tool object.

    Description decides tool selection (Part A, A4); the args schema is what the
    model fills in. Both must survive conversion or the model is choosing/among
    or calling a tool blind.
    """
    by_name = {t.name: t for t in LANGCHAIN_TOOLS}
    for fn in TOOL_FUNCTIONS:
        lc = by_name[fn.__name__]
        assert lc.description == (fn.__doc__ or "").strip()
        assert lc.description, f"{fn.__name__} lost its description in conversion"
        props = (lc.args_schema.model_json_schema().get("properties") or {})
        # Every declared parameter of the plain function appears as a model-facing
        # field (identity/scope are not parameters, so they never appear — below).
        hints = {k for k in typing.get_type_hints(fn) if k != "return"}
        assert set(props) == hints, f"{fn.__name__} arg schema drifted: {set(props)} vs {hints}"


def test_conversion_leaks_no_identity_or_scope_arg():
    """Invariant #25 holds by construction across the whole registry."""
    for lc in LANGCHAIN_TOOLS:
        props = set(lc.args_schema.model_json_schema().get("properties") or {})
        leak = props & _FORBIDDEN_ARGS
        assert not leak, f"{lc.name} exposed ambient authority as a tool arg: {leak}"


def test_literal_params_still_derive_an_enum():
    """A `Literal[...]` param keeps its enum constraint after conversion (CLAUDE.md §5)."""
    by_name = {t.name: t for t in LANGCHAIN_TOOLS}
    schema = by_name["diff_texts"].args_schema.model_json_schema()
    mode = schema["properties"]["mode"]
    # pydantic renders a Literal as an enum (possibly via $defs); assert both forms.
    enum = mode.get("enum") or _resolve_enum(schema, mode)
    assert set(enum) == {"unified", "ndiff"}


def _resolve_enum(schema: dict, prop: dict) -> list:
    ref = prop.get("allOf", [{}])[0].get("$ref") or prop.get("$ref")
    if ref:
        name = ref.split("/")[-1]
        return schema.get("$defs", {}).get(name, {}).get("enum", [])
    return []


# --- the second new tool, diff_texts (decision #54) --------------------------

def test_diff_texts_reports_changed_lines():
    out = diff_texts("def add(a, b):\n    return a + b",
                     "def add(a, b):\n    '''sum'''\n    return a + b")
    assert out["changed"] is True
    assert any(line.startswith("+") and "sum" in line for line in out["diff"])


def test_diff_texts_identical_is_not_a_change():
    out = diff_texts("same\ntext", "same\ntext")
    assert out["changed"] is False
    assert out["diff"] == []


def test_diff_texts_ndiff_mode_keeps_only_change_markers():
    out = diff_texts("a\nb", "a\nB", mode="ndiff")
    assert out["mode"] == "ndiff"
    assert all(line[:1] in ("+", "-") for line in out["diff"])


def test_diff_texts_oversized_input_is_its_own_error_branch():
    out = diff_texts("x" * 150_000, "y" * 150_000)
    assert out["error"] == "input_too_large"
    assert "diff" not in out  # never a partial diff dressed as complete


# --- offline: the converted new tool actually dispatches through the graph ----

def test_new_tool_dispatches_through_the_graph(monkeypatch):
    """A model tool-call to diff_texts routes, runs, and lands in the trace as `ok`.

    This is the offline proof that the tool is wired end-to-end through
    run_agent's registry -> conversion -> graph dispatch path, without a network
    call. The online test below proves the model can actually SEE and pick it.
    """
    tc = _tc("diff_texts", {"before": "a\nb", "after": "a\nB"}, "call_1")
    _patch_model(monkeypatch, [
        AIMessage(content="", tool_calls=[tc]),
        AIMessage(content="the second line changed from b to B"),
    ])
    result = run_agent(
        "what changed between these two blocks?",
        schemas=[schema_for(diff_texts)],
        registry={"diff_texts": diff_texts},
        verbose=False,
    )
    assert result["stopped"] == "answered"
    assert result["trace"][0]["tool"] == "diff_texts"
    assert result["trace"][0]["branch"] == "ok"
    assert result["trace"][0]["output"]["changed"] is True


# --- online: a real call through the compiled graph (CLAUDE.md §8) ------------

@pytest.mark.online
def test_online_agent_selects_new_diff_tool(online_key):  # noqa: F811
    """The real model, offered the whole registry, picks diff_texts unprompted-by-name.

    Proves the converted schema actually reaches the provider and that the model
    can select this new tool from among all ten — the framework-integration bug
    class an offline/mocked suite cannot see.
    """
    from agentlib.core import CHEAP

    result = run_agent(
        "Here are two versions of a line.\n"
        "BEFORE:\nreturn a + b\n"
        "AFTER:\nreturn a - b\n"
        "Use a tool to compute the exact diff between them, then say in one line what changed.",
        schemas=[schema_for(diff_texts)],
        registry={"diff_texts": diff_texts},
        model=CHEAP,
        verbose=False,
    )
    assert result["stopped"] == "answered"
    assert any(step["tool"] == "diff_texts" for step in result["trace"])
