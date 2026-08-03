"""tools — registry + schema assembly for the knowledge-graph tools.

Owner: Berat (T0.7). Imports every tool stub so the system runs end-to-end from
day one: schemas are handed to the model, the registry dispatches calls. In
Phase 0 each tool raises NotImplementedError, so the loop runs the full ORAV
path and fails only when a tool body is reached (the Phase 0 definition of done).

`build_registry()` returns `(schemas, registry)`:
  schemas   list[dict] from schema_for(fn) — passed to call(tools=...)
  registry  name -> callable — how loop.run_agent dispatches a tool call

The order below is the order the model sees the tools in. Descriptions (docstrings)
decide selection, not list order (Part A, A4) — but read tool is listed before
write tool so the safe path reads first.
"""

from __future__ import annotations

from typing import Any, Callable

from agentlib.langchain_tools import build_langchain_tools
from agentlib.schemas import schema_for

from .decisions import (
    append_decision_record,
    retrieve_decisions,
    verify_graph_integrity,
)
from .graph_query import query_component_graph
from .graph_write import prune_graph_node
from .memory_tools import retrieve_memory, save_memory
from .repo_scan import scan_repository_structure
from .text_tools import diff_texts
from .utility_tools import evaluate_expression

# Every callable tool, in the order the model sees them. Reads and appends
# before the destructive prune.
#
# The two lookups sit next to each other on purpose, because the pair is the
# point: `query_component_graph` answers "what is this connected to" from the
# DERIVED layer, `retrieve_decisions` answers "why is it like this" from the
# AUTHORED overlay. Two lookups per question, never one merged store
# (ARCHITECTURE.md §6.1).
TOOL_FUNCTIONS: list[Callable] = [
    scan_repository_structure,
    query_component_graph,
    retrieve_decisions,
    retrieve_memory,
    append_decision_record,
    save_memory,
    verify_graph_integrity,
    prune_graph_node,
    evaluate_expression,
    diff_texts,
]


def build_registry() -> tuple[list[dict[str, Any]], dict[str, Callable]]:
    """Assemble (schemas, registry) from TOOL_FUNCTIONS.

    Enum constraints on `kind`/`relation`/`status`/`scope`/`cascade` come for free
    from the tools' `Literal[...]` annotations via schema_for. Any further
    narrowing (numeric bounds on max_depth, "when NOT to call" prose) is authored
    by the tool owners in their own modules (T1.3, CLAUDE.md §1).
    """
    schemas = [schema_for(fn) for fn in TOOL_FUNCTIONS]
    registry = {fn.__name__: fn for fn in TOOL_FUNCTIONS}
    return schemas, registry


def build_langchain_registry() -> list:
    """The same TOOL_FUNCTIONS, registered through the ONE conversion point.

    HW4, decision #54. Every graph tool crosses into the framework here, in list
    order (the order the model sees them), via
    `agentlib.langchain_tools.build_langchain_tools` — which calls
    `to_langchain_tool` per function (decision #50). No tool is hand-wrapped as a
    LangChain `@tool`/`StructuredTool`: authors keep writing plain callables, and
    this is where they become tools the model can call. `agentlib.loop.run_agent`
    performs the identical conversion internally from its `registry` argument, so
    its frozen signature (decision #49) is untouched; this surface makes the
    conversion explicit and testable at the registry layer.

    Invariant #25 holds by construction: the conversion reads only the parameters
    the author already wrote, so no identity/scope argument can appear here that
    was not already a real function parameter (none is — scope stays ambient).
    """
    return build_langchain_tools(TOOL_FUNCTIONS)


# Module-level convenience: assembled once on import.
SCHEMAS, REGISTRY = build_registry()
LANGCHAIN_TOOLS = build_langchain_registry()

__all__ = [
    "build_registry",
    "build_langchain_registry",
    "SCHEMAS",
    "REGISTRY",
    "LANGCHAIN_TOOLS",
    "TOOL_FUNCTIONS",
]
