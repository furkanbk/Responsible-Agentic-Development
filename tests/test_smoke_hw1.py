"""tests/test_smoke_hw1.py — end-to-end smoke tests for HW1 (T2.5).

Owner: Dias Sarkytbaev (HW1). Amended by Berat for HW2, where the authored
decisions layer moved to the overlay — see the note on TestAppendDecisionRecord.

Renamed from `smoke_hw1.py` in HW2: the old name does not match pytest's
`test_*.py` discovery pattern, so `pytest tests/` silently collected none of
these. It only ever ran when named explicitly.

The model is SCRIPTED: `agentlib.loop.call` is monkeypatched with a fake that
replays a fixed sequence of `Result`s. That keeps the tests deterministic and
runnable offline while exercising the REAL loop, guards, gate, registry and
tools end to end. The live-model integration run is T8.3.

Covers (T2.5): corrupt fixture -> the error branch fires and the loop does not
treat it as data; gate declined -> the write is blocked; gate approved -> the
prune runs; max-step cap trips on a forced loop; stall detection; schema
constraints. The "seeded graph -> query returns expected node" case is no longer
skipped — T1.2 landed in PR #5.

Run:  python -m pytest tests/test_smoke_hw1.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentlib.loop as loop_mod
from agentlib.core import Result
from agentlib.guards import GATED, validate_args
from agentlib.loop import run_agent
from agentlib.schemas import schema_for
from tools import build_registry
from agentlib.session import session_scope
from overlay import db as overlay_db
from tools.decisions import (
    append_decision_record,
    retrieve_decisions,
    verify_graph_integrity,
)
from tools.graph_write import prune_graph_node

# --- fixtures & scripted-model helpers ---------------------------------------


def seeded_graph() -> dict:
    """A small healthy graph in the ARCHITECTURE.md §4 shape."""
    return {
        "nodes": [
            {"id": "agentlib.core", "path": "agentlib/core.py",
             "kind": "python", "symbols": ["call", "Result"]},
            {"id": "agentlib.loop", "path": "agentlib/loop.py",
             "kind": "python", "symbols": ["run_agent"]},
        ],
        "edges": [
            {"from": "agentlib.loop", "to": "agentlib.core", "relation": "imports"},
        ],
        "decisions": [
            {"component": "agentlib.core", "decision": "seed", "rationale": "seed",
             "status": "accepted", "ts": "2026-07-23T00:00:00+00:00"},
        ],
        "meta": {"scanned_at": "2026-07-23T00:00:00+00:00", "root": "."},
    }


@pytest.fixture
def graph_file(tmp_path, monkeypatch) -> Path:
    """Point the tools at a temporary, seeded graph file (decision #11)."""
    path = tmp_path / "knowledge_graph.json"
    monkeypatch.setenv("RADF_GRAPH_PATH", str(path))
    path.write_text(json.dumps(seeded_graph()), encoding="utf-8")
    return path


def scripted_call(responses):
    """A fake for agentlib.loop.call — replays `responses`, then answers."""
    queue = list(responses)

    def fake_call(*args, **kwargs):
        if queue:
            return queue.pop(0)
        return Result(text="(scripted) done", status="completed")

    return fake_call


def tool_call(name: str, arguments: dict, call_id: str = "c1") -> Result:
    """One scripted model turn that requests a single tool call."""
    item = {"type": "function_call", "name": name,
            "arguments": json.dumps(arguments), "call_id": call_id}
    return Result(
        tool_calls=[{"name": name, "arguments": arguments, "call_id": call_id}],
        output_items=[item],
        status="completed",
    )


def answer(text: str) -> Result:
    """One scripted model turn that answers with no tool call (done-signal)."""
    return Result(text=text, status="completed")


# --- T2.1 append_decision_record ---------------------------------------------


class TestAppendDecisionRecord:
    """Updated for HW2: decisions write to the overlay, not the JSON graph.

    The HW1 property under test is unchanged — an append never disturbs the
    derived layer — but it is now guaranteed by the two layers being in two
    different files rather than by this function remembering not to touch a key.
    """

    def test_writes_to_the_overlay_and_leaves_the_derived_layer_alone(self, graph_file):
        before = graph_file.read_bytes()
        with session_scope("berat"):
            rec = append_decision_record(
                "agentlib.loop", "test models are scripted",
                "offline determinism while team credits are pending",
                status="accepted",
            )
        assert rec["component"] == "agentlib.loop" and rec["ts"]
        assert rec["author_id"] == "berat"              # from the session, not the model
        assert rec["symbol_uid"] == "Module:agentlib.loop"
        assert graph_file.read_bytes() == before        # derived layer untouched

        conn = overlay_db.connect()
        rows = overlay_db.query_decisions(conn, user_id="berat")
        conn.close()
        assert [r["decision"] for r in rows] == ["test models are scripted"]

    def test_creates_the_overlay_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RADF_DB_PATH", str(tmp_path / "fresh.db"))
        with session_scope("berat"):
            rec = append_decision_record("x", "d", "r", status="proposed")
        assert "error" not in rec and (tmp_path / "fresh.db").exists()
        assert rec["status"] == "proposed"

    def test_a_corrupt_graph_no_longer_blocks_the_authored_layer(self, tmp_path, monkeypatch):
        """HW2 improvement: the two layers fail independently.

        Under HW1 a corrupt derived file blocked decision writes, because both
        layers shared it. Now a broken scan cannot stop an engineer recording
        why something is the way it is — and the corrupt file is still never
        rewritten (the rule behind decision #12).
        """
        path = tmp_path / "corrupt.json"
        path.write_text("{ this is not json", encoding="utf-8")
        monkeypatch.setenv("RADF_GRAPH_PATH", str(path))
        with session_scope("berat"):
            out = append_decision_record("x", "d", "r", status="accepted")
        assert "error" not in out
        assert path.read_text(encoding="utf-8") == "{ this is not json"

    def test_rejects_blank_fields_at_the_door(self, graph_file):
        with session_scope("berat"):
            out = append_decision_record("  ", "d", "r", status="proposed")
        assert out["error"] == "invalid_decision_record"

    def test_refuses_to_write_without_a_session(self, graph_file):
        """Identity comes from the runtime. No session, no attributable author."""
        out = append_decision_record("x", "d", "r", status="accepted")
        assert out["error"] == "no_session"

    def test_private_decisions_do_not_cross_users(self, graph_file):
        with session_scope("berat"):
            append_decision_record("agentlib.loop", "berat's own note", "taste",
                                   status="accepted", visibility="private")
        with session_scope("dias"):
            seen = retrieve_decisions("agentlib.loop")
        assert seen["count"] == 0


# --- T2.2 verify_graph_integrity ---------------------------------------------


class TestVerifyGraphIntegrity:
    def test_ok_on_a_healthy_graph(self, graph_file):
        # 2 nodes + 1 edge + 0 overlay decisions (the seed's `decisions[]` is a
        # legacy key the check no longer reads — the overlay is the authored layer).
        out = verify_graph_integrity(scope="all")
        assert out == {"ok": True, "scope": "all", "checked": 3}

    def test_missing_file_is_a_structured_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RADF_GRAPH_PATH", str(tmp_path / "absent.json"))
        out = verify_graph_integrity(scope="all")
        assert out["error"] == "graph_integrity_failed"
        assert "missing" in out["details"][0]

    def test_flags_orphan_edge_duplicate_id_and_orphaned_decision(
            self, tmp_path, monkeypatch):
        g = seeded_graph()
        g["nodes"].append({"id": "agentlib.core", "path": "dup.py",
                           "kind": "python", "symbols": []})       # duplicate id
        g["edges"].append({"from": "ghost", "to": "agentlib.core",
                           "relation": "imports"})                 # orphan edge
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(g), encoding="utf-8")
        monkeypatch.setenv("RADF_GRAPH_PATH", str(path))

        # The orphan now lives in the overlay: a decision about a component that
        # is no longer in the graph. This is the cross-store consistency check.
        with session_scope("berat"):
            append_decision_record("gone.module", "d", "r", status="accepted")

        out = verify_graph_integrity(scope="all")
        assert out["error"] == "graph_integrity_failed"
        blob = " | ".join(out["details"])
        assert "duplicate node id" in blob
        assert "orphan edge" in blob
        assert "orphaned decision" in blob            # surfaced, never deleted
        assert "Module:gone.module" in blob

        # Surfaced, NOT deleted — the record is still there to be reviewed.
        conn = overlay_db.connect()
        assert len(overlay_db.query_decisions(conn, user_id="berat")) == 1
        conn.close()

    def test_scope_filters_which_checks_run(self, tmp_path, monkeypatch):
        g = seeded_graph()
        g["edges"].append({"from": "ghost", "to": "agentlib.core",
                           "relation": "imports"})
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(g), encoding="utf-8")
        monkeypatch.setenv("RADF_GRAPH_PATH", str(path))
        assert verify_graph_integrity(scope="nodes")["ok"] is True
        assert verify_graph_integrity(scope="edges")["error"] == \
            "graph_integrity_failed"


# --- T2.3 prune_graph_node (called directly; the gate is loop-level) ----------


class TestPruneGraphNode:
    def test_node_and_edges_cascade(self, graph_file):
        out = prune_graph_node("agentlib.core", cascade="node_and_edges")
        assert out == {"removed": "agentlib.core", "edges_removed": 1,
                       "cascade": "node_and_edges"}
        stored = json.loads(graph_file.read_text(encoding="utf-8"))
        assert [n["id"] for n in stored["nodes"]] == ["agentlib.loop"]
        assert stored["edges"] == []
        # The authored layer is NEVER cascaded (decision #14) — the decision
        # referencing the pruned node survives as an orphan for review.
        assert len(stored["decisions"]) == 1

    def test_node_only_leaves_orphans_for_verify_to_flag(self, graph_file):
        out = prune_graph_node("agentlib.core", cascade="node_only")
        assert out["edges_removed"] == 0
        check = verify_graph_integrity(scope="edges")
        assert check["error"] == "graph_integrity_failed"  # the tools compose

    def test_unknown_node_is_a_structured_error(self, graph_file):
        out = prune_graph_node("no.such.node", cascade="node_only")
        assert out["error"] == "node_not_found"
        stored = json.loads(graph_file.read_text(encoding="utf-8"))
        assert len(stored["nodes"]) == 2                   # nothing was removed


# --- the loop end to end: error branch, gate, stopping conditions -------------


class TestLoopIntegration:
    def test_tool_error_reaches_the_loop_as_its_own_branch(
            self, tmp_path, monkeypatch):
        # Corrupt state: no graph file at all -> verify returns a structured
        # error -> the loop must tag it "error", not treat it as data (B2).
        monkeypatch.setenv("RADF_GRAPH_PATH", str(tmp_path / "absent.json"))
        schemas, registry = build_registry()
        monkeypatch.setattr(loop_mod, "call", scripted_call([
            tool_call("verify_graph_integrity", {"scope": "all"}),
            answer("The graph is missing — a scan is needed before answering."),
        ]))
        res = run_agent("is the graph healthy?", schemas, registry,
                        verbose=False)
        assert res["stopped"] == "answered"
        assert res["trace"][0]["branch"] == "error"
        assert res["trace"][0]["output"]["error"] == "graph_integrity_failed"

    def test_gate_declined_blocks_the_write(self, graph_file, monkeypatch):
        schemas, registry = build_registry()
        monkeypatch.setattr(loop_mod, "call", scripted_call([
            tool_call("prune_graph_node",
                      {"node_id": "agentlib.core", "cascade": "node_and_edges"}),
            answer("Understood — leaving agentlib.core in place."),
        ]))
        res = run_agent("prune agentlib.core", schemas, registry,
                        approve=lambda name, args: False, verbose=False)
        assert res["stopped"] == "answered"                # model reacted (B4)
        assert res["trace"][0]["branch"] == "declined"
        assert res["trace"][0]["output"] == {
            "declined_by_user": True, "tool": "prune_graph_node"}
        stored = json.loads(graph_file.read_text(encoding="utf-8"))
        assert any(n["id"] == "agentlib.core" for n in stored["nodes"])

    def test_gate_approved_lets_the_prune_run(self, graph_file, monkeypatch):
        schemas, registry = build_registry()
        approvals: list = []

        def approve(name, args):
            approvals.append((name, args))
            return True

        monkeypatch.setattr(loop_mod, "call", scripted_call([
            tool_call("prune_graph_node",
                      {"node_id": "agentlib.core", "cascade": "node_and_edges"}),
            answer("Pruned agentlib.core and its edges."),
        ]))
        res = run_agent("prune agentlib.core", schemas, registry,
                        approve=approve, verbose=False)
        assert approvals == [("prune_graph_node",
                              {"node_id": "agentlib.core",
                               "cascade": "node_and_edges"})]
        assert res["trace"][0]["branch"] == "ok"
        stored = json.loads(graph_file.read_text(encoding="utf-8"))
        assert all(n["id"] != "agentlib.core" for n in stored["nodes"])

    def test_invalid_enum_is_caught_before_the_tool_runs(
            self, graph_file, monkeypatch):
        schemas, registry = build_registry()
        monkeypatch.setattr(loop_mod, "call", scripted_call([
            tool_call("verify_graph_integrity", {"scope": "everything"}),
            answer("Retrying with a valid scope."),
        ]))
        res = run_agent("check the graph", schemas, registry, verbose=False)
        assert res["trace"][0]["branch"] == "invalid_args"
        assert res["trace"][0]["output"]["error"] == "invalid_args"

    def test_max_step_cap_trips_on_a_forced_loop(self, graph_file, monkeypatch):
        schemas, registry = build_registry()
        monkeypatch.setattr(loop_mod, "call", scripted_call([
            tool_call("verify_graph_integrity", {"scope": "nodes"}, "c1"),
            tool_call("verify_graph_integrity", {"scope": "edges"}, "c2"),
            tool_call("verify_graph_integrity", {"scope": "all"}, "c3"),
        ]))
        res = run_agent("audit forever", schemas, registry, max_steps=3,
                        verbose=False)
        assert res["stopped"] == "max_steps" and res["steps"] == 3

    def test_stall_detection_stops_identical_repeats(
            self, graph_file, monkeypatch):
        schemas, registry = build_registry()
        monkeypatch.setattr(loop_mod, "call", scripted_call([
            tool_call("verify_graph_integrity", {"scope": "all"}, "c1"),
            tool_call("verify_graph_integrity", {"scope": "all"}, "c2"),
        ]))
        res = run_agent("check twice", schemas, registry, verbose=False)
        assert res["stopped"] == "stalled"


# --- schema constraints (HW1: ≥1 constrained param per tool) ------------------


class TestSchemas:
    def test_literal_params_derive_enums(self):
        by_name = {s["name"]: s for s in
                   (schema_for(append_decision_record),
                    schema_for(verify_graph_integrity),
                    schema_for(prune_graph_node))}
        props = by_name["append_decision_record"]["parameters"]["properties"]
        assert props["status"]["enum"] == ["proposed", "accepted", "superseded"]
        props = by_name["verify_graph_integrity"]["parameters"]["properties"]
        assert props["scope"]["enum"] == ["nodes", "edges", "all"]
        schema = by_name["prune_graph_node"]
        assert schema["parameters"]["properties"]["cascade"]["enum"] == \
            ["node_only", "node_and_edges"]
        assert set(schema["parameters"]["required"]) == {"node_id", "cascade"}

    def test_descriptions_say_when_not_to_call(self):
        for fn in (append_decision_record, verify_graph_integrity,
                   prune_graph_node):
            assert "when not" in (fn.__doc__ or "").lower(), fn.__name__

    def test_bad_enum_is_rejected_by_validate_args(self):
        errs = validate_args(
            schema_for(append_decision_record),
            {"component": "x", "decision": "d", "rationale": "r",
             "status": "rejected"},          # not in the enum
        )
        assert errs and "not in" in errs[0]

    def test_only_the_irreversible_tools_are_gated(self):
        # HW2 (T7.1): apply_change overwrites files wholesale, so it joins GATED
        # beside prune_graph_node. Both are irreversible; nothing reversible is
        # gated (no ceremony where it is not earned — CLAUDE.md §5).
        assert GATED == {"prune_graph_node", "apply_change"}


# --- read path through the loop (T1.2 landed in PR #5) ------------------------


def test_seeded_graph_query_returns_expected_node(graph_file):
    from tools.graph_query import query_component_graph
    out = query_component_graph(component="agentlib.core",
                                relation="imported_by")
    assert out["found"] is True
    assert "agentlib.loop" in out["related"]
