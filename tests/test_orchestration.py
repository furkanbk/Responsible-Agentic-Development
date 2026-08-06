"""tests.test_orchestration — the two-agent pipeline (HW2, T6.3-T6.5).

Owner: Berat Furkan Kocak.

Run:  python -m pytest tests/test_orchestration.py -v

The planner is injected as a plain function here. That is deliberate: the
orchestrator's job is the BRANCHING, and the branching is what should be under
test. Whether Alejandro's planner produces a good impact set is
`tests/test_planner.py`'s question, and it should not be able to break these.
"""

from __future__ import annotations

import json

import pytest

from agentlib.runlog import RunLog, read_runs
from agentlib.session import current_impact_set, impact_scope
from agents.envelope import AgentResult, empty_plan
from orchestrator import PLAN_KEY, run_change_request
from overlay import db as overlay_db


def plan_with(**overrides) -> dict:
    plan = empty_plan()
    plan["impacted"] = ["Module:tools.decisions"]
    plan["steps"] = [{"path": "tools/decisions.py", "intent": "add a field"}]
    plan.update(overrides)
    return plan


def planner_returning(envelope: AgentResult):
    def _planner(request, **kwargs):
        return envelope
    return _planner


def recording_executor(captured: dict):
    """An executor that records what it was able to see, then succeeds."""
    def _executor(request, *, run_id, plan_key=PLAN_KEY, **kwargs):
        conn = overlay_db.connect()
        try:
            captured["plan"] = overlay_db.scratch_read(
                conn, run_id=run_id, agent="executor", step=0, key=plan_key
            )
            captured["run_id"] = run_id
        finally:
            conn.close()
        return AgentResult.ok({"plan": captured["plan"], "changes": []},
                              agent="executor", notes="did the thing")
    return _executor


class TestBranching:
    """One branch per status. None of them reads prose."""

    @pytest.mark.parametrize("envelope,should_run_executor", [
        (AgentResult.needs_input(["which module?"], agent="planner"), False),
        (AgentResult.blocked("a decision forbids it", agent="planner"), False),
        (AgentResult.failed("graph unreadable", agent="planner"), False),
        (AgentResult.ok(plan_with(), agent="planner"), True),
    ])
    def test_the_executor_runs_only_on_an_actionable_plan(self, envelope, should_run_executor):
        ran = {"called": False}

        def _executor(request, **kwargs):
            ran["called"] = True
            return AgentResult.ok({"changes": []}, agent="executor")

        out = run_change_request(
            "change something", user_id="berat", verbose=False,
            planner=planner_returning(envelope), executor=_executor,
        )
        assert ran["called"] is should_run_executor
        assert out["status"] == ("ok" if should_run_executor else envelope.status)

    def test_a_plan_needing_approval_does_not_reach_the_executor(self):
        """`ok` is not permission — the gate is separate from the status."""
        ran = {"called": False}

        def _executor(request, **kwargs):
            ran["called"] = True
            return AgentResult.ok({}, agent="executor")

        envelope = AgentResult.ok(plan_with(), agent="planner", needs_approval=True)
        run_change_request("x", user_id="berat", verbose=False,
                           planner=planner_returning(envelope), executor=_executor)
        assert ran["called"] is False

    def test_open_questions_are_surfaced_verbatim(self):
        envelope = AgentResult.needs_input(["which decisions module?"], agent="planner")
        out = run_change_request("x", user_id="berat", verbose=False,
                                 planner=planner_returning(envelope),
                                 executor=lambda *a, **k: pytest.fail("must not run"))
        assert out["planner"]["result"]["open_questions"] == ["which decisions module?"]


class TestSharedMemoryHandover:
    def test_the_plan_travels_through_scratch_not_an_argument(self):
        captured: dict = {}
        out = run_change_request(
            "change something", user_id="berat", verbose=False,
            planner=planner_returning(AgentResult.ok(plan_with(), agent="planner")),
            executor=recording_executor(captured),
        )
        # The executor never received the plan as a parameter; it read it back.
        assert captured["plan"] == plan_with()
        assert captured["run_id"] == out["run_id"]

    def test_the_read_set_records_which_write_was_observed(self):
        """The causal chain: which version of the plan did the executor act on?"""
        captured: dict = {}
        out = run_change_request(
            "x", user_id="berat", verbose=False,
            planner=planner_returning(AgentResult.ok(plan_with(), agent="planner")),
            executor=recording_executor(captured),
        )
        conn = overlay_db.connect()
        dump = overlay_db.scratch_dump(conn, out["run_id"])
        conn.close()

        writes = [w for w in dump["writes"] if w["key"] == PLAN_KEY]
        reads = [r for r in dump["reads"] if r["key"] == PLAN_KEY]
        assert len(writes) == 1 and len(reads) == 1
        assert reads[0]["saw_seq"] == writes[0]["seq"]
        assert writes[0]["agent"] == "planner" and reads[0]["agent"] == "executor"

    def test_scratch_history_lands_in_the_run_log(self):
        run_change_request(
            "x", user_id="berat", verbose=False,
            planner=planner_returning(AgentResult.ok(plan_with(), agent="planner")),
            executor=recording_executor({}),
        )
        record = read_runs()[-1]
        assert record["scratch"]["writes"], "the handover is invisible in the log"
        assert record["scratch"]["reads"]


class TestRunLog:
    def test_both_envelopes_are_recorded(self):
        run_change_request(
            "x", user_id="berat", thread_id="t7", verbose=False,
            planner=planner_returning(AgentResult.ok(plan_with(), agent="planner")),
            executor=recording_executor({}),
        )
        record = read_runs()[-1]
        agents = [e["agent"] for e in record["envelopes"]]
        assert agents == ["planner", "executor"]
        assert record["user_id"] == "berat" and record["thread_id"] == "t7"

    def test_a_run_that_stops_at_the_planner_is_still_logged(self):
        """A monitor that only sees successful runs is grading a filtered sample."""
        run_change_request(
            "x", user_id="berat", verbose=False,
            planner=planner_returning(AgentResult.failed("boom", agent="planner")),
            executor=lambda *a, **k: pytest.fail("must not run"),
        )
        record = read_runs()[-1]
        assert record["stopped"] == "failed"
        assert [e["agent"] for e in record["envelopes"]] == ["planner"]


class TestImpactScope:
    def test_the_impact_set_is_ambient_and_restored(self):
        assert current_impact_set() == ()
        with impact_scope(["Module:a", "Module:b"]):
            assert current_impact_set() == ("Module:a", "Module:b")
        assert current_impact_set() == ()

    def test_no_plan_means_no_writes_not_unrestricted_writes(self):
        """The default has to deny. Empty is not a wildcard."""
        assert current_impact_set() == ()


class TestExecutorGuards:
    """The executor's own refusals, without a model in the loop."""

    def test_a_missing_plan_is_a_failure_not_an_empty_run(self):
        from agents.executor import run_executor

        conn = overlay_db.connect()
        run_id = overlay_db.start_run(conn, user_id="berat", thread_id="t",
                                      agent="orchestrator")
        conn.close()
        out = run_executor("do it", run_id=run_id, verbose=False)
        assert out.status == "failed" and "no plan" in out.notes

    def test_open_questions_stop_the_executor_in_code_not_only_in_the_brief(self):
        from agents.executor import run_executor

        conn = overlay_db.connect()
        run_id = overlay_db.start_run(conn, user_id="berat", thread_id="t",
                                      agent="orchestrator")
        overlay_db.scratch_write(
            conn, run_id=run_id, agent="planner", step=1, key=PLAN_KEY,
            value=plan_with(open_questions=["which module?"]),
        )
        conn.close()
        out = run_executor("do it", run_id=run_id, verbose=False)
        assert out.status == "needs_input"
        assert out.result["open_questions"] == ["which module?"]

    def test_a_malformed_plan_is_rejected_before_any_model_call(self):
        from agents.executor import run_executor

        conn = overlay_db.connect()
        run_id = overlay_db.start_run(conn, user_id="berat", thread_id="t",
                                      agent="orchestrator")
        overlay_db.scratch_write(conn, run_id=run_id, agent="planner", step=1,
                                 key=PLAN_KEY, value={"nonsense": True})
        conn.close()
        out = run_executor("do it", run_id=run_id, verbose=False)
        assert out.status == "failed" and "malformed" in out.notes

    def test_an_empty_step_list_asks_rather_than_reporting_success(self):
        """An empty step list is not a failure — but it is not `ok` either.

        Amended from `test_an_empty_step_list_is_ok_not_a_failure` (decision
        #78). The original intent — "this is not a fault" — still holds and is
        still asserted: `needs_input` is the ask-a-human branch, not the failure
        branch. What changed is that `ok` was ALSO wrong: it propagates to the
        orchestrator's final status, so a human who asked for a change was told
        `status: ok` for a run that touched no file. Seen live when the model
        named a seed but returned steps with empty `path`, which `_clean_steps`
        drops.
        """
        from agents.executor import run_executor

        conn = overlay_db.connect()
        run_id = overlay_db.start_run(conn, user_id="berat", thread_id="t",
                                      agent="orchestrator")
        overlay_db.scratch_write(conn, run_id=run_id, agent="planner", step=1,
                                 key=PLAN_KEY, value=plan_with(steps=[]))
        conn.close()
        out = run_executor("do it", run_id=run_id, verbose=False)
        assert out.status == "needs_input"      # asks, never claims success
        assert out.status != "failed"           # the original point, preserved
        assert out.result["changes"] == []


class TestReadSourceConfinement:
    """Reading is reversible, but an unconfined reader is still a file-read
    primitive pointed at a repo containing `.env`."""

    def test_reads_a_real_repo_file(self):
        from tools.read_source import read_source_file

        out = read_source_file("agents/envelope.py")
        assert "AgentResult" in out["content"] and out["lines"] > 10

    @pytest.mark.parametrize("path", [
        "../../../etc/passwd",          # traversal out of the repo
        "agents/../../etc/passwd",      # traversal that looks repo-relative
        ".env",                         # secrets
        ".git/config",                  # git internals
        "store/radf.db",                # the agent's own overlay
        "overlay/db.py",                # ...and the code behind it
    ])
    def test_refuses_out_of_scope_paths(self, path):
        from tools.read_source import read_source_file

        assert read_source_file(path)["error"] == "path_outside_scope"

    def test_a_missing_file_is_its_own_error(self):
        from tools.read_source import read_source_file

        assert read_source_file("agents/nope.py")["error"] == "file_missing"

    def test_truncation_is_declared_not_silent(self, tmp_path, monkeypatch):
        """A model that edits a file it only half saw deletes the other half."""
        import tools.read_source as rs

        monkeypatch.setattr(rs, "MAX_BYTES", 50)
        out = rs.read_source_file("agents/envelope.py")
        assert out["truncated"] is True and len(out["content"]) == 50


class TestBrief:
    def test_the_brief_is_pushed_and_names_its_boundaries(self):
        from agents.executor import load_brief

        brief = load_brief()
        for required in ("Scope", "What you may decide alone", "When you must ask",
                         "When you must escalate", "Effort budget"):
            assert required in brief

    def test_the_executor_toolset_is_narrow_by_construction(self):
        from agents.executor import build_executor_registry

        _, registry = build_executor_registry()
        assert set(registry) == {"read_source_file", "query_component_graph",
                                 "retrieve_decisions", "apply_change"}
        # Not merely discouraged in prose — absent.
        for forbidden in ("scan_repository_structure", "prune_graph_node",
                          "save_memory", "append_decision_record"):
            assert forbidden not in registry
