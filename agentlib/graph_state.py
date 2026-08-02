"""agentlib.graph_state — the LangGraph state schema (HW4, decision #51).

`AgentState` is an explicit `TypedDict`, not an ad-hoc dict threaded through node
functions (the professor's stated requirement). Its fields are exactly what
`agentlib/loop.py` already tracked as local variables before the refactor:
`messages` is the model's view of the conversation; `trace`/`signatures`/
`declined_signatures`/`step` are the loop's own bookkeeping; `stopped`/`answer`
are the terminal result. Nothing here is new behavior — it's the pre-existing
shape given a name LangGraph can reduce over.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """One run's state as it flows through the graph.

    messages              conversation so far, reduced with `add_messages` (each
                           node appends; LangGraph merges by message id).
    trace                 one dict per executed/declined tool call, each tagged
                           with its branch ("ok" | "error" | "declined" |
                           "invalid_args") — same shape `loop.py` returned before.
    signatures             call_signature(name, args) for every tool call made
                           this run, newest last — stall detection (guards.py).
    declined_signatures     signatures the human has declined this run.
    step                   completed reasoning steps so far (the max_steps floor).
    stopped                terminal stopping condition, or None while running:
                           answered | max_steps | stalled | declined | truncated.
    answer                 the model's final text, or None if it never answered.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    trace: list[dict[str, Any]]
    signatures: list[str]
    declined_signatures: list[str]
    step: int
    stopped: Optional[str]
    answer: Optional[str]
