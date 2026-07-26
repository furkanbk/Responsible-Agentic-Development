"""tools.repo_scan — repository -> knowledge graph.

Owner: Alejandro Ramírez Trueba (Phase 1, T1.1). The function signature, its
docstring, and the return shape below are the stub contract authored in Phase 0
(T0.8) and are unchanged — only the body is filled in (CLAUDE.md §1).

Structural extraction is hand-rolled with `ast` this round (CLAUDE.md §4): parse
each Python module, read its top-level symbols and `import` statements, and emit
`nodes` + `edges`. This is DERIVED data — a scan regenerates the structural half
of the graph wholesale and never touches the authored `decisions` layer
(CLAUDE.md §6, ARCHITECTURE.md §4). Graph-file I/O reuses the Phase 2 helpers
owned by tools.decisions (repo-root-anchored path, `RADF_GRAPH_PATH` override,
atomic writes) rather than re-implementing them.
"""

from __future__ import annotations

import ast
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# Graph-file I/O is shared Phase 2 plumbing owned by tools.decisions.
from .decisions import (
    _graph_path,
    _load_graph,
    _save_graph,
    migrate_legacy_decisions,
)

# Directories never worth indexing — envs, caches, VCS, and the runtime graph.
_SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    "node_modules", "store", ".idea", ".vscode", ".mypy_cache",
}

# Guardrail bound for max_depth (a scan must never runaway-recurse). Enforced in
# the body because validate_args only checks the integer TYPE, not the range.
_MAX_DEPTH_CEILING = 64


def _extensions_for(kind: str) -> tuple[str, ...]:
    """File suffixes to index for a given `kind`."""
    if kind == "python":
        return (".py",)
    if kind == "markdown":
        return (".md",)
    return (".py", ".md")  # "any"


def _module_id(root: Path, file: Path) -> str:
    """Dotted module id for a Python file, relative to the scan root.

    `agentlib/core.py` -> `agentlib.core`; a package `__init__.py` collapses to
    the package id (`tools/__init__.py` -> `tools`). This is the id convention
    the graph and the decision overlay join on (ARCHITECTURE.md §4, T1.5).
    """
    rel = file.relative_to(root).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)


def _doc_id(root: Path, file: Path) -> str:
    """POSIX relative-path id for a non-Python (markdown) file."""
    return file.relative_to(root).as_posix()


def _top_level_symbols(tree: ast.Module) -> list[str]:
    """Names of top-level functions and classes defined in a module."""
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
    return out


def _import_targets(tree: ast.Module, self_id: str) -> set[str]:
    """Module names this file imports, as candidate node ids.

    Handles `import a.b`, `from a.b import c` (yields both `a.b` and `a.b.c` so a
    submodule import resolves), and relative `from . import x` / `from ..a import
    y` (resolved against `self_id`'s package). Whether a candidate becomes an edge
    is decided later by matching against the set of known node ids.
    """
    package = self_id.rsplit(".", 1)[0] if "." in self_id else ""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: climb `level` packages from self
                base = package.split(".") if package else []
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                prefix = ".".join([*base, node.module]) if node.module else ".".join(base)
            else:
                prefix = node.module or ""
            if prefix:
                targets.add(prefix)
                for alias in node.names:
                    targets.add(f"{prefix}.{alias.name}")
    targets.discard(self_id)
    return targets


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
    # --- validate args at the door -> structured error branch, never a crash ---
    if kind not in ("python", "markdown", "any"):
        return {"error": "invalid_args",
                "details": [f"kind={kind!r} not in ('python','markdown','any')"]}
    if (not isinstance(max_depth, int) or isinstance(max_depth, bool)
            or not (0 <= max_depth <= _MAX_DEPTH_CEILING)):
        return {"error": "invalid_args",
                "details": [f"max_depth must be an int in [0, {_MAX_DEPTH_CEILING}], "
                            f"got {max_depth!r}"]}
    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        return {"error": "invalid_root",
                "details": [f"{root!r} is not an existing directory"]}
    root_path = root_path.resolve()

    # --- refuse to run over a corrupt graph (decision #12): a scan PRESERVES the
    #     authored decisions layer, so it must be able to read the file first ----
    path = _graph_path()
    existing = _load_graph(path)
    if existing is None:
        return {"error": "graph_unreadable",
                "details": [f"{path} exists but is not valid JSON — refusing to "
                            "overwrite (would lose the authored decisions layer)"]}

    exts = _extensions_for(kind)
    nodes: list[dict] = []
    node_ids: set[str] = set()
    raw_imports: dict[str, set[str]] = {}

    # --- walk + extract (pass 1: nodes) ---------------------------------------
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        depth = len(Path(dirpath).relative_to(root_path).parts)
        if depth >= max_depth:
            dirnames[:] = []  # do not descend past the depth bound
        if depth > max_depth:
            continue
        for fname in sorted(filenames):
            file = Path(dirpath) / fname
            if file.suffix not in exts:
                continue
            if file.suffix == ".py":
                try:
                    tree = ast.parse(file.read_text(encoding="utf-8"))
                except (SyntaxError, OSError, UnicodeDecodeError):
                    continue  # a file we cannot parse is skipped, not fatal
                nid = _module_id(root_path, file)
                nodes.append({
                    "id": nid,
                    "path": file.relative_to(root_path).as_posix(),
                    "kind": "python",
                    "symbols": _top_level_symbols(tree),
                })
                node_ids.add(nid)
                raw_imports[nid] = _import_targets(tree, nid)
            else:  # markdown
                nid = _doc_id(root_path, file)
                nodes.append({"id": nid, "path": nid, "kind": "markdown", "symbols": []})
                node_ids.add(nid)

    # --- resolve internal edges (pass 2): keep only edges between known nodes ---
    edges: list[dict] = []
    for src in sorted(raw_imports):
        for tgt in sorted(raw_imports[src]):
            if tgt in node_ids:
                edges.append({"from": src, "to": tgt, "relation": "imports"})

    # --- write: replace the DERIVED layer wholesale ---------------------------
    # As of HW2 this file holds NOTHING BUT derived data — the authored
    # decisions moved to the overlay (store/radf.db), which a scan cannot reach.
    # The layer separation is now enforced by the filesystem rather than by this
    # function remembering to preserve a key (CLAUDE.md §6).
    scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing["nodes"] = nodes
    existing["edges"] = edges
    existing["meta"] = {"scanned_at": scanned_at, "root": str(root)}
    # Rescue any HW1 decisions still sitting in the JSON before the key goes.
    # Migrating first means a scan can never be the thing that loses them.
    migrate_legacy_decisions(existing)
    existing.pop("decisions", None)
    _save_graph(path, existing)

    return {"nodes": len(nodes), "edges": len(edges), "root": str(root),
            "kind": kind, "scanned_at": scanned_at}
