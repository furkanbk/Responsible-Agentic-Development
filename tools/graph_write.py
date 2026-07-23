"""tools.graph_write — destructive graph edits.

Owner: Dias Sarkytbaev (Phase 2, T2.3). Implemented against the stub contract
authored in Phase 0 (T0.8): signature, constrained params, and return shape
are unchanged.

`prune_graph_node` is the ONE irreversible action in HW1. It is listed in
agentlib.guards.GATED and proceeds only on explicit human approval (Part B,
B4). The gate itself lives in the loop; this module's job is to keep the blast
radius exact — remove precisely what was asked, count what was removed, and
never touch the authored decisions layer (CLAUDE.md §6, decision #14).
"""

from __future__ import annotations

from typing import Literal

# Graph-file I/O is shared Phase 2 plumbing owned by tools.decisions (same owner).
from .decisions import _graph_path, _load_graph, _save_graph


def prune_graph_node(
    node_id: str,
    cascade: Literal["node_only", "node_and_edges"],
) -> dict:
    """Permanently remove a node from the knowledge graph. IRREVERSIBLE.

    This deletes derived structural data that cannot be recovered without a full
    re-scan, so it is GATED (agentlib.guards.GATED) and runs only after the user
    has EXPLICITLY confirmed at the approval gate. Reversible tools are ungated;
    this one is not (CLAUDE.md §5).

    Constrained params:
      node_id  required; the id of the node to remove.
      cascade  enum: "node_only" (leave edges — may create orphan edges that
               verify_graph_integrity will then flag) or "node_and_edges" (also
               remove every edge touching the node). Default-cascade behavior is
               an open question in ARCHITECTURE.md §5 — Dias records the decision.

    When NOT to call: do not call this to "clean up" without a confirmed user
    request. Never call it to work around stale data — re-scan instead. Use only
    after the user has explicitly confirmed the removal.

    Returns (contract): a removal summary, e.g.
        {"removed": <node_id>, "edges_removed": <int>, "cascade": <str>}
    """
    path = _graph_path()
    if not path.exists():
        return {
            "error": "node_not_found",
            "node_id": node_id,
            "details": [f"graph file missing at {path} — nothing to prune"],
        }
    graph = _load_graph(path)
    if graph is None:
        return {
            "error": "graph_unreadable",
            "details": [f"{path} exists but is not valid JSON — refusing to "
                        "modify it (decision #12)"],
        }

    nodes = graph.get("nodes") or []
    keep = [n for n in nodes
            if not (isinstance(n, dict) and n.get("id") == node_id)]
    if len(keep) == len(nodes):
        # An honest miss is a structured error, not a fake removal summary.
        return {"error": "node_not_found", "node_id": node_id}
    graph["nodes"] = keep

    edges_removed = 0
    if cascade == "node_and_edges":
        edges = graph.get("edges") or []
        kept_edges = [
            e for e in edges
            if not (isinstance(e, dict)
                    and (e.get("from") == node_id or e.get("to") == node_id))
        ]
        edges_removed = len(edges) - len(kept_edges)
        graph["edges"] = kept_edges
    # cascade == "node_only": edges stay put — any now-orphaned edge is exactly
    # what verify_graph_integrity flags afterwards (decision #14, the two tools
    # compose). The authored `decisions` layer is NEVER cascaded either way: a
    # pruned component's decisions become orphans, surfaced for review, not
    # deleted (CLAUDE.md §6).

    _save_graph(path, graph)
    return {"removed": node_id, "edges_removed": edges_removed, "cascade": cascade}
