"""tests.test_envelope — the frozen inter-agent contract (HW2, T6.1).

Owner: Berat Furkan Kocak.

Small on purpose. These tests exist so that if someone changes a field name,
the build breaks here — loudly, in one place — rather than in Alejandro's
planner and Dias's monitor separately and confusingly.
"""

from __future__ import annotations

import pytest

from agents.envelope import PLAN_KEYS, AgentResult, empty_plan, validate_plan


class TestStatuses:
    def test_ok_is_actionable(self):
        assert AgentResult.ok({"x": 1}, agent="planner").actionable

    def test_approval_makes_an_ok_result_not_actionable(self):
        """The gate is the loop's, not the agent's — `ok` is not permission."""
        assert not AgentResult.ok({}, needs_approval=True).actionable

    def test_blocked_and_failed_are_distinct(self):
        """A correct refusal is not a defect, and the trace must say which."""
        blocked = AgentResult.blocked("a decision forbids this")
        failed = AgentResult.failed("the tool errored")
        assert blocked.status != failed.status
        assert not blocked.actionable and not failed.actionable

    def test_needs_input_carries_its_questions_in_the_result(self):
        env = AgentResult.needs_input(["which module owns this?"], agent="planner")
        assert env.status == "needs_input"
        assert env.result["open_questions"] == ["which module owns this?"]


class TestRoundTrip:
    def test_survives_the_shared_scratch(self):
        original = AgentResult.ok(empty_plan(), agent="planner", notes="hi")
        assert AgentResult.from_dict(original.to_dict()) == original

    def test_an_unknown_status_raises_rather_than_coercing(self):
        """Silently coercing would route the run down the wrong branch."""
        with pytest.raises(ValueError):
            AgentResult.from_dict({"status": "probably_fine"})


class TestPlanShape:
    def test_empty_plan_has_every_frozen_key(self):
        assert set(empty_plan()) == set(PLAN_KEYS)

    def test_a_well_formed_plan_validates(self):
        plan = empty_plan()
        plan["impacted"] = ["Module:tools.decisions"]
        plan["steps"] = [{"path": "tools/decisions.py", "intent": "add a field"}]
        assert validate_plan(plan) == []

    def test_a_raw_component_string_is_rejected(self):
        """Catching this here is what keeps the GitNexus swap a uid remap."""
        plan = empty_plan()
        plan["impacted"] = ["tools.decisions"]
        assert any("resolve_uid" in p for p in validate_plan(plan))

    def test_a_missing_key_is_reported_not_defaulted(self):
        plan = empty_plan()
        del plan["impacted"]
        assert any("missing required key: impacted" in p for p in validate_plan(plan))

    def test_a_step_without_a_path_is_rejected(self):
        plan = empty_plan()
        plan["steps"] = [{"intent": "do something"}]
        assert validate_plan(plan)
