"""tools.graph_query — knowledge graph -> answers (STUB CONTRACT).

Owner: Alejandro Ramírez Trueba (Phase 1, T1.2). Stub contract authored in
Phase 0 (T0.8). Read-only and reversible -> ungated. Do NOT change the signature,
constrained params, or return shape without agreement (CLAUDE.md §1).
"""

from __future__ import annotations

from typing import Literal


def query_component_graph(
    component: str,
    relation: Literal["imports", "imported_by", "neighbors", "all"],
) -> dict:
    """Look up a component and its relations in the knowledge graph. Read-only.

    Reads store/knowledge_graph.json (never writes). Reversible, so ungated — no
    approval ceremony (CLAUDE.md §5).

    Constrained params:
      component  required; the node id / module path to look up.
      relation   enum: "imports" (what it imports), "imported_by" (its dependents),
                 "neighbors" (both directions), or "all" (node + every edge).

    When NOT to call: do not call this to (re)build the graph — if the component
    is not present because the graph is empty or stale, that is a scan job for
    `scan_repository_structure`, not a query (T1.4).

    Returns (contract): a lookup result, e.g.
        {"component": <str>, "relation": <str>, "found": <bool>,
         "node": {<node fields>} | None, "related": [<component id>, ...]}
    """
    raise NotImplementedError("Phase 1 (Alejandro): implement query_component_graph")
