"""tools.graph_write — destructive graph edits (STUB CONTRACT).

Owner: Dias Sarkytbaev (Phase 2, T2.3). Stub contract authored in Phase 0 (T0.8).

`prune_graph_node` is the ONE irreversible action in HW1. It is listed in
agentlib.guards.GATED and proceeds only on explicit human approval (Part B, B4).
Do NOT change the signature, constrained params, or return shape without
agreement (CLAUDE.md §1).
"""

from __future__ import annotations

from typing import Literal


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
    raise NotImplementedError("Phase 2 (Dias): implement prune_graph_node")
