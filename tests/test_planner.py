"""tests/test_planner.py — the planner agent (HW2, T6.2c).

Owner: Alejandro Ramírez Trueba. New file (no existing owner).

The planner is the second agent — the one that reads the graph so the executor
does not have to. Its interesting properties are all deterministic and testable
without a live model:

  * the impact set is TRANSITIVE on `imported_by`, and CAPPED (T6.2a);
  * `open_questions` is EARNED — a component not in the graph, or two decisions
    that contradict each other, and never defaulted to empty (T6.2b);
  * the plan dict is well-formed against the frozen contract.

Most tests pass a `component_hint`, which skips the single model call, so the
graph walk and conflict logic are exercised in isolation. One test scripts the
model (monkeypatching `agents.planner.call`, the same trick the smoke suite
uses) to prove the free-form path resolves a seed and reaches the same walk.

Run:  python -m pytest tests/test_planner.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _online import online_key  # noqa: F401 — pytest fixture, used by name

import agents.planner as planner_mod
from agentlib.core import Result
from agents.envelope import validate_plan
from agents.planner import run_planner
from overlay import db as overlay_db
from overlay.uid import resolve_uid
from tools.repo_scan import scan_repository_structure


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def graph(tmp_path):
    """A scanned synthetic repo, written to the (conftest-isolated) graph store.

    Import edges:  top -> pkg, top -> pkg.a, pkg.a -> pkg.b
    So `imported_by` fans OUT the other way:
        pkg.b  <- pkg.a  <- top
    which is exactly the transitive chain the impact walk must follow.
    """
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (src / "pkg" / "a.py").write_text("import pkg.b\n", encoding="utf-8")
    (src / "pkg" / "b.py").write_text("VALUE = 1\n", encoding="utf-8")
    (src / "top.py").write_text("from pkg import a\n", encoding="utf-8")
    summary = scan_repository_structure(str(src), max_depth=3, kind="python")
    assert summary["nodes"] >= 4
    return src


def seed_decision(component, decision, rejected=None, *, status="accepted"):
    """Insert one team decision straight into the overlay (bypasses the tool,
    which does not expose `rejected`)."""
    conn = overlay_db.connect()
    try:
        row = overlay_db.insert_decision(
            conn, component=component, decision=decision,
            rationale="seeded by test", status=status, author_id="hw1",
            rejected=rejected,
        )
    finally:
        conn.close()
    return row["decision_id"]


# --- the transitive, capped impact set ---------------------------------------


class TestImpactSet:
    def test_impact_is_transitive_on_imported_by(self, graph):
        out = run_planner("change B", component_hint="pkg.b", max_hops=2, verbose=False)
        assert out.status == "ok"
        # pkg.b itself, its importer pkg.a, and its importer's importer top.
        assert set(out.result["impacted"]) == {
            "Module:pkg.b", "Module:pkg.a", "Module:top"}
        assert out.result["impact_max_hops"] == 2
        assert validate_plan(out.result) == []

    def test_the_cap_bounds_the_walk(self, graph):
        out = run_planner("change B", component_hint="pkg.b", max_hops=1, verbose=False)
        # One hop reaches pkg.a but not top.
        assert set(out.result["impacted"]) == {"Module:pkg.b", "Module:pkg.a"}
        assert out.result["impact_max_hops"] == 1

    def test_a_leaf_change_impacts_only_itself(self, graph):
        out = run_planner("tweak top", component_hint="top", max_hops=2, verbose=False)
        # Nothing imports top, so the impact set is just top.
        assert out.result["impacted"] == ["Module:top"]

    def test_the_step_and_plan_shape_are_well_formed(self, graph):
        out = run_planner("change B", component_hint="pkg/b.py", verbose=False)
        assert out.result["steps"] == [{"path": "pkg/b.py", "intent": "change B"}]
        assert out.status == "ok" and out.needs_approval is False


# --- open questions are earned, never defaulted ------------------------------


class TestOpenQuestions:
    def test_a_component_absent_from_the_graph_stops_the_run(self, graph):
        out = run_planner("change it", component_hint="does.not.exist", verbose=False)
        assert out.status == "needs_input"
        assert out.result["open_questions"]
        assert "not in the knowledge graph" in out.result["open_questions"][0]

    def test_contradicting_decisions_land_in_open_questions(self, graph):
        # One decision adopts what the other explicitly rejected, on pkg.b.
        a = seed_decision("pkg.b", "use JSON storage")
        b = seed_decision("pkg.b", "use SQLite", rejected="use JSON storage")
        out = run_planner("change B", component_hint="pkg.b", max_hops=0, verbose=False)

        assert out.status == "needs_input"
        q = " ".join(out.result["open_questions"])
        assert "conflicting decisions" in q
        assert a in q and b in q
        # Both decision ids are still recorded as constraints on the plan.
        assert set(out.result["constraints"]) == {a, b}

    def test_agreeing_decisions_do_not_trip_a_conflict(self, graph):
        seed_decision("pkg.b", "use SQLite")
        seed_decision("pkg.b", "index on symbol_uid")
        out = run_planner("change B", component_hint="pkg.b", max_hops=0, verbose=False)
        assert out.status == "ok" and out.result["open_questions"] == []
        assert len(out.result["constraints"]) == 2


# --- the free-form (scripted model) path -------------------------------------


class TestScriptedModel:
    def test_the_model_seed_reaches_the_same_walk(self, graph, monkeypatch):
        scripted = Result(text='{"seed": "pkg.b", '
                               '"steps": [{"path": "pkg/b.py", "intent": "bump"}]}')
        monkeypatch.setattr(planner_mod, "call", lambda *a, **k: scripted)

        out = run_planner("please change the B module", max_hops=2, verbose=False)
        assert out.status == "ok"
        assert set(out.result["impacted"]) == {
            "Module:pkg.b", "Module:pkg.a", "Module:top"}
        assert out.result["steps"] == [{"path": "pkg/b.py", "intent": "bump"}]

    def test_unparseable_model_output_is_a_failure_not_a_guess(self, graph, monkeypatch):
        monkeypatch.setattr(planner_mod, "call",
                            lambda *a, **k: Result(text="sorry, I can't help"))
        out = run_planner("do something", verbose=False)
        assert out.status == "failed"

    def test_a_truncated_proposal_is_a_failure(self, graph, monkeypatch):
        monkeypatch.setattr(planner_mod, "call",
                            lambda *a, **k: Result(text='{"seed": "pkg', truncated=True))
        out = run_planner("do something", verbose=False)
        assert out.status == "failed"


# --- online: a real model resolving the seed (HW4, T13b / CLAUDE.md §8) --------


@pytest.mark.online
def test_online_a_real_model_seed_produces_a_well_formed_plan(graph, online_key):
    """One real model call on the planner's free-form path.

    `TestScriptedModel` above pins the same path with a canned reply, which
    proves the walk but never proves a live model can produce a seed this code
    accepts — a scripted `Result` always parses, by construction. §8 wants that
    gap covered, so this asks the real model against the synthetic repo the
    `graph` fixture scans.

    Asserted: the plan is STRUCTURALLY valid and its impact set is real — every
    uid it names resolves to a node the scan actually found. Not asserted: which
    seed the model picks. A live model may reasonably read "the b module" as
    `pkg.b` or ask a question instead, and `needs_input` is a correct outcome
    here (T6.2b), not a failure — so both are accepted and only a malformed plan
    or a hallucinated component fails the test.
    """
    out = run_planner("please plan a change to the b module inside pkg",
                      max_hops=2, verbose=False)

    assert out.status in ("ok", "needs_input"), f"live planner failed: {out.notes}"
    assert validate_plan(out.result) == []

    if out.status == "ok":
        impacted = out.result["impacted"]
        assert impacted, "an ok plan with an empty impact set authorises nothing"
        known = {resolve_uid(m) for m in ("pkg.a", "pkg.b", "pkg", "top")}
        assert set(impacted) <= known, f"planner invented components: {impacted}"
