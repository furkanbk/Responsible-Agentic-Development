"""tests/test_read_path.py — Phase 1 read-path tests (T1.1 / T1.2).

Owner: Alejandro Ramírez Trueba. New file (no existing owner), so it is created
here rather than editing tools-owner Dias's tests/smoke_hw1.py.

Two levels of testing, mirroring how the smoke suite is built:

  1. Unit — the two read tools in isolation against a temporary graph file
     (RADF_GRAPH_PATH override, decision #11): scan extraction/depth/kind, the
     decisions-preservation invariant, query relations, and every structured
     error branch (bad root, bad max_depth, corrupt graph, missing component).

  2. Integration — the REAL run_agent loop over the REAL tools, with only the
     model SCRIPTED (agentlib.loop.call monkeypatched, same trick as smoke_hw1).
     Proves scan -> query -> answer runs the ORAV loop to `answered`, and that a
     tool's structured error reaches the loop as its own `error` branch instead
     of flowing back as valid data (Part B, B2; CLAUDE.md §5).

Run:  python -m pytest tests/test_read_path.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentlib.loop as loop_mod
from agentlib.core import Result
from agentlib.loop import run_agent
from tools import build_registry
from tools.graph_query import query_component_graph
from tools.repo_scan import scan_repository_structure


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def graph_env(tmp_path, monkeypatch) -> Path:
    """Point the tools at a temporary graph file; return its path."""
    path = tmp_path / "knowledge_graph.json"
    monkeypatch.setenv("RADF_GRAPH_PATH", str(path))
    return path


@pytest.fixture
def sample_repo(tmp_path) -> Path:
    """A tiny synthetic source tree to scan.

    Layout (ids relative to the tree root):
        pkg/__init__.py   -> node id "pkg"
        pkg/a.py          -> "pkg.a",  import pkg.b
        pkg/b.py          -> "pkg.b"
        top.py            -> "top",    from pkg import a
        README.md         -> "README.md" (markdown)
    Expected python edges: top->pkg, top->pkg.a, pkg.a->pkg.b.
    """
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (src / "pkg" / "a.py").write_text(
        "import pkg.b\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    (src / "pkg" / "b.py").write_text("class B:\n    pass\n", encoding="utf-8")
    (src / "top.py").write_text(
        "from pkg import a\n\n\nclass Top:\n    pass\n", encoding="utf-8")
    (src / "README.md").write_text("# readme\n", encoding="utf-8")
    return src


# --- scripted-model helpers (same shape as tests/smoke_hw1.py) ---------------


def tool_call(name: str, arguments: dict, call_id: str = "c1") -> Result:
    """One scripted model turn requesting a single tool call."""
    item = {"type": "function_call", "name": name,
            "arguments": json.dumps(arguments), "call_id": call_id}
    return Result(
        tool_calls=[{"name": name, "arguments": arguments, "call_id": call_id}],
        output_items=[item], status="completed",
    )


def answer(text: str) -> Result:
    """One scripted model turn that answers with no tool call (done-signal)."""
    return Result(text=text, status="completed")


def scripted_call(responses):
    """A fake for agentlib.loop.call — replays `responses`, then answers."""
    queue = list(responses)

    def fake_call(*args, **kwargs):
        return queue.pop(0) if queue else answer("(scripted) done")

    return fake_call


# --- unit: scan_repository_structure -----------------------------------------


class TestScan:
    def test_extracts_nodes_edges_and_symbols(self, sample_repo, graph_env):
        summ = scan_repository_structure(str(sample_repo), max_depth=3, kind="python")
        assert summ["kind"] == "python" and summ["scanned_at"]

        g = json.loads(graph_env.read_text(encoding="utf-8"))
        ids = {n["id"] for n in g["nodes"]}
        assert ids == {"pkg", "pkg.a", "pkg.b", "top"}
        edges = {(e["from"], e["to"]) for e in g["edges"]}
        assert edges == {("top", "pkg"), ("top", "pkg.a"), ("pkg.a", "pkg.b")}
        assert all(e["relation"] == "imports" for e in g["edges"])
        top = next(n for n in g["nodes"] if n["id"] == "top")
        assert top["symbols"] == ["Top"] and top["path"] == "top.py"

    def test_max_depth_bounds_the_walk(self, sample_repo, graph_env):
        summ = scan_repository_structure(str(sample_repo), max_depth=0, kind="python")
        g = json.loads(graph_env.read_text(encoding="utf-8"))
        ids = {n["id"] for n in g["nodes"]}
        assert ids == {"top"}                     # pkg/ (depth 1) was not descended
        assert summ["nodes"] == 1 and g["edges"] == []

    def test_kind_filters_which_files_are_indexed(self, sample_repo, graph_env):
        scan_repository_structure(str(sample_repo), max_depth=3, kind="markdown")
        g = json.loads(graph_env.read_text(encoding="utf-8"))
        assert {n["id"] for n in g["nodes"]} == {"README.md"}
        assert g["nodes"][0]["kind"] == "markdown" and g["edges"] == []

        scan_repository_structure(str(sample_repo), max_depth=3, kind="any")
        g = json.loads(graph_env.read_text(encoding="utf-8"))
        assert "README.md" in {n["id"] for n in g["nodes"]}
        assert "pkg.a" in {n["id"] for n in g["nodes"]}

    def test_rescan_replaces_derived_but_preserves_decisions(self, sample_repo, graph_env):
        # Seed a graph with an AUTHORED decision + stale derived data.
        graph_env.write_text(json.dumps({
            "nodes": [{"id": "stale", "path": "stale.py", "kind": "python", "symbols": []}],
            "edges": [], "decisions": [{"component": "pkg.a", "decision": "keep me",
                                        "rationale": "authored", "status": "accepted",
                                        "ts": "2026-07-23T00:00:00+00:00"}],
            "meta": {},
        }), encoding="utf-8")

        scan_repository_structure(str(sample_repo), max_depth=3, kind="python")
        g = json.loads(graph_env.read_text(encoding="utf-8"))
        assert "stale" not in {n["id"] for n in g["nodes"]}   # derived replaced wholesale
        assert len(g["decisions"]) == 1                        # authored layer preserved
        assert g["decisions"][0]["decision"] == "keep me"

    def test_invalid_root_is_a_structured_error(self, graph_env):
        out = scan_repository_structure("/no/such/dir", max_depth=2, kind="python")
        assert out["error"] == "invalid_root"
        assert not graph_env.exists()                          # nothing was written

    def test_invalid_max_depth_is_a_structured_error(self, sample_repo, graph_env):
        assert scan_repository_structure(str(sample_repo), -1, "python")["error"] == "invalid_args"
        assert scan_repository_structure(str(sample_repo), 999, "python")["error"] == "invalid_args"
        # bool is not an acceptable int here (True == 1 would silently pass otherwise)
        assert scan_repository_structure(str(sample_repo), True, "python")["error"] == "invalid_args"

    def test_refuses_to_overwrite_a_corrupt_graph(self, sample_repo, graph_env):
        graph_env.write_text("{ not json", encoding="utf-8")
        out = scan_repository_structure(str(sample_repo), max_depth=3, kind="python")
        assert out["error"] == "graph_unreadable"
        assert graph_env.read_text(encoding="utf-8") == "{ not json"   # untouched


# --- unit: query_component_graph ---------------------------------------------


class TestQuery:
    @pytest.fixture
    def scanned(self, sample_repo, graph_env):
        scan_repository_structure(str(sample_repo), max_depth=3, kind="python")
        return graph_env

    def test_imports_and_imported_by(self, scanned):
        assert query_component_graph("pkg.a", "imports")["related"] == ["pkg.b"]
        assert query_component_graph("pkg.b", "imported_by")["related"] == ["pkg.a"]

    def test_neighbors_and_all_span_both_directions(self, scanned):
        got = query_component_graph("pkg.a", "neighbors")
        assert got["found"] and set(got["related"]) == {"pkg.b", "top"}
        assert set(query_component_graph("pkg.a", "all")["related"]) == {"pkg.b", "top"}

    def test_lookup_by_path_resolves(self, scanned):
        out = query_component_graph("pkg/a.py", "imports")
        assert out["found"] and out["node"]["id"] == "pkg.a"

    def test_missing_component_is_found_false_not_an_error(self, scanned):
        out = query_component_graph("does.not.exist", "all")
        assert out["found"] is False and out["node"] is None and "error" not in out

    def test_absent_graph_is_found_false_not_an_error(self, graph_env):
        out = query_component_graph("anything", "all")
        assert out["found"] is False and "error" not in out

    def test_corrupt_graph_is_a_structured_error(self, graph_env):
        graph_env.write_text("{ not json", encoding="utf-8")
        assert query_component_graph("anything", "all")["error"] == "graph_unreadable"


# --- integration: the two tools inside the REAL loop -------------------------


class TestReadPathInLoop:
    def test_scan_then_query_runs_the_loop_to_answered(
            self, sample_repo, graph_env, monkeypatch):
        schemas, registry = build_registry()
        monkeypatch.setattr(loop_mod, "call", scripted_call([
            tool_call("scan_repository_structure",
                      {"root": str(sample_repo), "max_depth": 3, "kind": "python"}),
            tool_call("query_component_graph",
                      {"component": "pkg.b", "relation": "imported_by"}, call_id="c2"),
            answer("pkg.b is imported by pkg.a"),
        ]))
        result = run_agent("who imports pkg.b?", schemas, registry, verbose=False)

        assert result["stopped"] == "answered"
        assert [t["branch"] for t in result["trace"]] == ["ok", "ok"]
        scan_out = result["trace"][0]["output"]
        assert scan_out["nodes"] == 4 and scan_out["edges"] == 3
        assert result["trace"][1]["output"]["related"] == ["pkg.a"]

    def test_tool_error_reaches_the_loop_as_its_own_branch(
            self, graph_env, monkeypatch):
        # Corrupt graph -> query returns a structured error, NOT a fake answer.
        graph_env.write_text("{ not json", encoding="utf-8")
        schemas, registry = build_registry()
        monkeypatch.setattr(loop_mod, "call", scripted_call([
            tool_call("query_component_graph", {"component": "x", "relation": "all"}),
            answer("the graph is unreadable — a scan is needed first"),
        ]))
        result = run_agent("what imports x?", schemas, registry, verbose=False)

        err_events = [t for t in result["trace"] if t["branch"] == "error"]
        assert len(err_events) == 1
        assert err_events[0]["output"]["error"] == "graph_unreadable"
        # The loop kept control and did not treat the error as a valid result.
        assert result["stopped"] == "answered"
