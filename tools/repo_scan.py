"""tools.repo_scan — repository -> knowledge graph (STUB CONTRACT).

Owner: Alejandro Ramírez Trueba (Phase 1, T1.1). This file is a stub contract
authored in Phase 0 (T0.8): the signature, docstring, and return shape are the
interface the rest of the system is built against. Do NOT change the signature,
the docstring's constrained params, or the return shape without agreement
(CLAUDE.md §1). Fill in the body — replace the NotImplementedError.
"""

from __future__ import annotations

from typing import Literal


def scan_repository_structure(
    root: str,
    max_depth: int,
    kind: Literal["python", "markdown", "any"],
) -> dict:
    """Walk the repo from `root` and (re)write the structural half of the graph.

    Extracts modules and their import edges (use `ast` for Python, not regex) and
    writes `nodes` + `edges` to store/knowledge_graph.json. This regenerates
    DERIVED structural data — it may overwrite existing nodes/edges wholesale and
    must never touch the authored `decisions` layer (CLAUDE.md §6).

    Constrained params (constrain the derived schema per T1.3):
      root       required; the directory to scan.
      max_depth  integer; bound recursion depth (add a numeric bound in the schema).
      kind       enum: which files to index — "python", "markdown", or "any".

    When NOT to call: do not call this to answer a question about a component that
    is already in the graph — use `query_component_graph` instead. Only scan when
    the graph is missing, stale, or the tree changed (Part A, A4; T1.4).

    Returns (contract): a summary dict, e.g.
        {"nodes": <int>, "edges": <int>, "root": <str>, "kind": <str>,
         "scanned_at": <iso8601 str>}
    """
    raise NotImplementedError("Phase 1 (Alejandro): implement scan_repository_structure")
