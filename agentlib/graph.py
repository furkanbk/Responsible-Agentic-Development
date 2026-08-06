"""agentlib.graph — the LangGraph orchestration core (HW4, decisions #49, #52).

Builds a two-node `StateGraph` over `agentlib.graph_state.AgentState` that
reproduces `agentlib/loop.py`'s pre-refactor observe -> reason -> act -> verify
loop exactly:

    agent (LLM + bind_tools) --tool_calls--> tools --no stop--> agent
      |--no tool_calls / truncated--> END          |--stall/decline/max_steps--> END

The `tools` node does not reimplement any guard: it calls straight into
`agentlib/guards.py` (`validate_args`, `requires_approval`, `is_error_result`,
`detect_stall`, `call_signature`) — the same functions `loop.py` called before
the refactor. Only the control flow moved from a `while True:` loop to a graph.

The approval gate stays the existing synchronous `approve(name, args) -> bool`
callback (decision #52) — LangGraph's native `interrupt()` is not used here;
see the decision record for why.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from tracing.spans import (
    BRANCH_ATTR,
    GATED_ATTR,
    set_attributes,
    set_outputs,
    tool_span,
)

from .core import BASE_URL, CHEAP
from .graph_state import AgentState
from .guards import (
    call_signature,
    detect_stall,
    is_error_result,
    requires_approval,
    validate_args,
)

_API_KEY_ENV = "OPENCODE_API_KEY"


def _build_chat_model(model: str, temperature: Optional[float] = None) -> ChatOpenAI:
    """One `ChatOpenAI` pointed at the same Zen Responses endpoint `core.call` uses.

    `use_responses_api=True` matters: Zen's base URL is a Responses-API-shaped
    endpoint (see `agentlib.core`'s docstring), not `/chat/completions`. Verified
    empirically — without it, tool-call args and truncation metadata don't match
    what `_route_after_agent` below expects.
    """
    import os

    api_key = os.environ.get(_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"{_API_KEY_ENV} is not set. Copy .env.example to .env and fill it in "
            "(the key is never hard-coded; see CLAUDE.md)."
        )
    # `temperature=None` means "send no temperature" — the provider default, which
    # is what every run before HW6 used. It is NOT a synonym for 0.0, and the
    # difference is the whole of HW6 requirement 6: three runs at a pinned 0.0 are
    # identical, so no scenario can ever be shown as flaky (decision #92).
    extra = {} if temperature is None else {"temperature": temperature}
    return ChatOpenAI(
        base_url=BASE_URL, api_key=api_key, model=model, use_responses_api=True, **extra
    )


def _is_truncated(ai: AIMessage) -> bool:
    """Mirror `agentlib.guards.check_output`'s ERROR_BRANCH test for the Responses API.

    `agentlib.core._to_result` treats `status == "incomplete" and stop_reason ==
    "max_output_tokens"` as truncated (decision #9); `response_metadata` carries
    the same two fields under `status` / `incomplete_details.reason` when the
    call goes through `ChatOpenAI(use_responses_api=True)`.
    """
    meta = ai.response_metadata or {}
    if meta.get("status") != "incomplete":
        return False
    details = meta.get("incomplete_details") or {}
    reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
    return reason == "max_output_tokens"


def _make_agent_node(chat_model: ChatOpenAI, tools: list[StructuredTool]) -> Callable[[AgentState], dict]:
    bound = chat_model.bind_tools(tools) if tools else chat_model

    def agent_node(state: AgentState) -> dict:
        ai = bound.invoke(state["messages"])
        if _is_truncated(ai):
            return {"messages": [ai], "stopped": "truncated", "answer": None}
        if not ai.tool_calls:
            return {"messages": [ai], "stopped": "answered", "answer": ai.text}
        return {"messages": [ai], "stopped": None}

    return agent_node


def _make_tools_node(
    schema_by_name: dict[str, dict[str, Any]],
    registry: dict[str, Callable],
    approve: Optional[Callable[[str, dict], bool]],
    max_steps: int,
) -> Callable[[AgentState], dict]:
    def tools_node(state: AgentState) -> dict:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)

        trace = list(state["trace"])
        signatures = list(state["signatures"])
        declined_signatures = set(state["declined_signatures"])
        new_messages: list[BaseMessage] = []
        stopped: Optional[str] = None

        for tc in last.tool_calls:
            name, args, call_id = tc["name"], tc["args"] or {}, tc["id"]
            sig = call_signature(name, args)
            signatures.append(sig)

            # Stall guard: an identical call already made this run. Matches
            # loop.py exactly — stop mid-batch, do not process later calls.
            if detect_stall(signatures):
                stopped = "declined" if sig in declined_signatures else "stalled"
                break

            schema = schema_by_name.get(name)

            # HW6 (T15.3): the one place a tool is dispatched is the one place a
            # TOOL span is opened. `mlflow.langchain.autolog()` cannot do this —
            # this node is hand-written, not LangChain's `ToolNode`, so autolog
            # sees one opaque "tools" step and the trajectory adapter would read
            # nothing (decision #88). The span carries the guards' `branch` too:
            # a safety scorer must not have to re-derive from arguments what the
            # code already decided.
            with tool_span(name, args) as span:
                if schema is None:
                    out, branch = {"error": "unknown_tool", "tool": name}, "invalid_args"
                elif errs := validate_args(schema, args):
                    out, branch = {"error": "invalid_args", "tool": name, "details": errs}, "invalid_args"
                elif requires_approval(name):
                    if approve is not None and approve(name, args):
                        out = registry[name](**args)
                        branch = "error" if is_error_result(out) else "ok"
                    else:
                        out, branch = {"declined_by_user": True, "tool": name}, "declined"
                        declined_signatures.add(sig)
                else:
                    out = registry[name](**args)
                    branch = "error" if is_error_result(out) else "ok"

                set_attributes(span, {BRANCH_ATTR: branch, GATED_ATTR: requires_approval(name)})
                set_outputs(span, {"result": out})

            trace.append({"tool": name, "args": args, "output": out, "branch": branch})
            new_messages.append(
                ToolMessage(content=json.dumps({"result": out}, default=str), tool_call_id=call_id)
            )

        step = state["step"]
        if stopped is None:
            step += 1
            if step >= max_steps:
                stopped = "max_steps"

        return {
            "messages": new_messages,
            "trace": trace,
            "signatures": signatures,
            "declined_signatures": list(declined_signatures),
            "step": step,
            "stopped": stopped,
        }

    return tools_node


def _route_after_agent(state: AgentState) -> str:
    return END if state["stopped"] is not None else "tools"


def _route_after_tools(state: AgentState) -> str:
    return END if state["stopped"] is not None else "agent"


def build_graph(
    tools: list[StructuredTool],
    schema_by_name: dict[str, dict[str, Any]],
    registry: dict[str, Callable],
    approve: Optional[Callable[[str, dict], bool]] = None,
    model: str = CHEAP,
    max_steps: int = 8,
    temperature: Optional[float] = None,
) -> CompiledStateGraph:
    """Compile the `agent` <-> `tools` graph. One compiled graph per `run_agent` call.

    `tools` are LangChain tools (via `agentlib.langchain_tools.build_langchain_tools`),
    bound to the model so it decides when to call one from the user query — never
    hardcoded routing. `schema_by_name` still comes from `agentlib.schemas.schema_for`
    on the same underlying functions: `validate_args` (guards.py) is unchanged and
    needs that JSON-Schema shape, not LangChain's.
    """
    # The second argument is passed ONLY when there is a temperature to pass.
    # Several suites (`test_smoke_hw1.py`, `test_loop_contract.py`, ...) replace
    # `_build_chat_model` with a one-argument stub, and CLAUDE.md §8 keeps the
    # pre-refactor suites unamended — so the untraced, untemperatured path has to
    # call it exactly as it did before HW6, arity included.
    chat_model = (_build_chat_model(model) if temperature is None
                  else _build_chat_model(model, temperature))
    graph = StateGraph(AgentState)
    graph.add_node("agent", _make_agent_node(chat_model, tools))
    graph.add_node("tools", _make_tools_node(schema_by_name, registry, approve, max_steps))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", _route_after_tools, {"agent": "agent", END: END})
    return graph.compile()
