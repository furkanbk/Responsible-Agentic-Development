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

from agentlib.schemas import schema_for

from .decisions import append_decision_record, verify_graph_integrity
from .graph_query import query_component_graph
from .graph_write import prune_graph_node
from .repo_scan import scan_repository_structure

# Every callable tool, in the order the model sees them. Read/append/verify
# before the destructive prune.
TOOL_FUNCTIONS: list[Callable] = [
    scan_repository_structure,
    query_component_graph,
    append_decision_record,
    verify_graph_integrity,
    prune_graph_node,
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


# Module-level convenience: assembled once on import.
SCHEMAS, REGISTRY = build_registry()

__all__ = ["build_registry", "SCHEMAS", "REGISTRY", "TOOL_FUNCTIONS"]
