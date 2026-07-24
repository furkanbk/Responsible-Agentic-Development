"""tools.graph_query — knowledge graph -> answers.

Owner: Alejandro Ramírez Trueba (Phase 1, T1.2). The function signature, its
docstring, and the return shape are the stub contract authored in Phase 0
(T0.8) and are unchanged — only the body is filled in (CLAUDE.md §1).

Read-only and reversible -> ungated (CLAUDE.md §5): a lookup can never damage
the graph, so it earns no approval ceremony. Reads through the shared Phase 2
I/O helper owned by tools.decisions. A corrupt graph is the one failure this
tool surfaces as a structured error for the loop to branch on (Part B, B2);
an absent/empty graph is a legitimate `found: false` answer, not an error —
"go scan first" is steered by the docstring, not by raising.
"""

from __future__ import annotations

from typing import Literal

# Graph-file I/O is shared Phase 2 plumbing owned by tools.decisions.
from .decisions import _graph_path, _load_graph


def _find_node(nodes: list, component: str) -> dict | None:
    """Resolve `component` to a node by id first, then by path."""
    for n in nodes:
        if isinstance(n, dict) and n.get("id") == component:
            return n
    for n in nodes:
        if isinstance(n, dict) and n.get("path") == component:
            return n
    return None


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
    path = _graph_path()
    graph = _load_graph(path)
    if graph is None:
        # Corrupt file -> structured error the loop branches on (never dressed as
        # a real answer). Distinct from an ABSENT graph, which _load_graph maps to
        # the empty shape and we answer as found: false below.
        return {"error": "graph_unreadable",
                "details": [f"{path} exists but is not valid JSON"]}

    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]

    node = _find_node(nodes, component)
    if node is None:
        return {"component": component, "relation": relation, "found": False,
                "node": None, "related": []}

    nid = node.get("id")
    imports = [e["to"] for e in edges if e.get("from") == nid and "to" in e]
    imported_by = [e["from"] for e in edges if e.get("to") == nid and "from" in e]

    if relation == "imports":
        related = imports
    elif relation == "imported_by":
        related = imported_by
    else:  # "neighbors" and "all" both span both directions
        related = imports + imported_by

    # Stable, de-duplicated ordering so identical queries give identical output.
    related = sorted(dict.fromkeys(related))
    return {"component": component, "relation": relation, "found": True,
            "node": node, "related": related}
