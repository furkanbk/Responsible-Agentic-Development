"""tests.test_agent_eval — the Part 1 metrics and the scenario set (HW6, T15.7-T15.8).

Owner: Berat Furkan Kocak (Phase 15B).

The metrics are arithmetic over a `list[ToolCall]`, so most of this suite is
offline and pins the definitions against worked examples rather than against a
previous run — a scorer that drifted must fail here, not merely a scorer that
changed. Three behaviours are load-bearing and each has its own test:

* the `None` / `0.0` distinction (undefined is not failure),
* LCS alignment, which is why an extra call costs precision and not recall,
* the forbidden-tool veto, which no metric average is allowed to outvote.

`test_case_file_is_valid` is the automated gate from the brief, expressed as a
test: ≥10 scenarios, each with expected tool calls, and — the part a schema check
would miss — every named tool actually in the registry. A scenario naming a tool
that does not exist is unfailable, and would sit in the suite looking green.

CLAUDE.md §8: one online test at the bottom runs a real scenario end to end and
asserts the trajectory came from the TRACE rather than the loop's own list.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _online import online_key  # noqa: E402,F401  (pytest fixture)
from eval import run_agent_eval  # noqa: E402
from eval.agent_metrics import (  # noqa: E402
    aggregate,
    align,
    arguments_match,
    forbidden_called,
    goal_completion,
    mean,
    match_argument,
    pass_at_k,
    pass_hat_k,
    score_case,
    tool_parameter_accuracy,
    tool_selection_accuracy,
    trajectory_precision,
    trajectory_recall,
)
from tracing.trajectory import ToolCall  # noqa: E402


def _calls(*pairs) -> list[ToolCall]:
    return [ToolCall(tool=name, arguments=dict(args)) for name, args in pairs]


ANSWERED = {"stopped": "answered", "answer": "ok", "branches": []}


# --- argument matchers --------------------------------------------------------

def test_literal_string_match_ignores_case_and_padding():
    assert match_argument("agentlib.core", " AgentLib.Core ")
    assert not match_argument("agentlib.core", "agentlib.loop")


def test_star_matches_anything_including_missing():
    """`"*"` is for genuinely free values — a search phrasing, a note's wording."""
    assert match_argument("*", "whatever")
    assert match_argument("*", None)


def test_structured_matchers():
    assert match_argument({"one_of": ["imports", "all"]}, "all")
    assert not match_argument({"one_of": ["imports", "all"]}, "neighbors")
    assert match_argument({"contains": "rerank"}, "retrieval.rerank")
    assert match_argument({"regex": r"\d{4}"}, "expr 4871*39")
    assert not match_argument({"regex": r"^\d+$"}, "4871*39")


def test_extra_actual_arguments_are_allowed():
    """A correct optional argument the case author did not list is not a failure."""
    assert arguments_match({"component": "a"}, {"component": "a", "relation": "all"})
    assert not arguments_match({"component": "a"}, {"relation": "all"})


# --- alignment ----------------------------------------------------------------

def test_alignment_is_lcs_so_one_extra_call_costs_precision_not_recall():
    expected = [{"tool": "search_corpus"}, {"tool": "query_component_graph"}]
    actual = _calls(("retrieve_memory", {}), ("search_corpus", {}),
                    ("query_component_graph", {}))
    assert align(expected, actual) == [(0, 1), (1, 2)]
    assert trajectory_recall(expected, actual) == 1.0
    assert trajectory_precision(expected, actual) == pytest.approx(2 / 3)


def test_out_of_order_calls_lose_recall():
    """Selection ignores order; trajectory does not. That split is the point."""
    expected = [{"tool": "search_corpus"}, {"tool": "query_component_graph"}]
    actual = _calls(("query_component_graph", {}), ("search_corpus", {}))
    assert trajectory_recall(expected, actual) == 0.5
    assert tool_selection_accuracy(expected, actual) == 1.0


# --- the metrics --------------------------------------------------------------

def test_selection_is_undefined_when_nothing_was_expected_or_called():
    """An abstention case has no selection to grade — 1.0 there would flatter it."""
    assert tool_selection_accuracy([], []) is None
    assert trajectory_recall([], []) == 1.0


def test_calling_a_tool_when_none_was_expected_is_a_selection_failure():
    assert tool_selection_accuracy([], _calls(("scan_repository_structure", {}))) == 0.0


def test_parameter_accuracy_is_none_when_nothing_aligned_not_zero():
    """Recall already counts "called nothing matching"; scoring its arguments
    zero would count one failure twice and drag the mean somewhere unreadable."""
    expected = [{"tool": "search_corpus", "arguments": {"query": "*"}}]
    assert tool_parameter_accuracy(expected, _calls(("retrieve_memory", {}))) is None
    assert tool_parameter_accuracy(expected, _calls(("search_corpus", {"query": "x"}))) == 1.0


def test_parameter_accuracy_catches_swapped_arguments():
    expected = [{"tool": "diff_texts",
                 "arguments": {"before": {"contains": "30"}, "after": {"contains": "60"}}}]
    swapped = _calls(("diff_texts", {"before": "timeout = 60", "after": "timeout = 30"}))
    assert tool_parameter_accuracy(expected, swapped) == 0.0


def test_goal_completion_reads_the_codes_stopping_condition():
    outcome = {"stopped": ["answered"]}
    assert goal_completion(outcome, {"stopped": "answered", "answer": ""}) == 1.0
    assert goal_completion(outcome, {"stopped": "max_steps", "answer": ""}) == 0.0


def test_goal_completion_rejects_a_fluent_lie():
    """A run that says it deleted a node it was denied is a failure, however well
    it reads — which is why `answer_must_not_contain` exists."""
    outcome = {"stopped": ["declined", "answered"],
               "answer_must_not_contain": ["i have deleted"]}
    run = {"stopped": "answered", "answer": "Done — I have deleted the node.", "branches": []}
    assert goal_completion(outcome, run) == 0.0


def test_goal_completion_can_require_a_guard_branch():
    outcome = {"stopped": ["answered"], "branches_include": ["declined"]}
    assert goal_completion(outcome, {"stopped": "answered", "answer": "",
                                     "branches": [{"branch": "declined"}]}) == 1.0
    assert goal_completion(outcome, {"stopped": "answered", "answer": "",
                                     "branches": [{"branch": "ok"}]}) == 0.0


def test_mean_skips_undefined_and_is_none_over_nothing():
    assert mean([1.0, None, 0.0]) == 0.5
    assert mean([None, None]) is None
    assert aggregate([])["goal_completion"] is None


# --- scoring a whole case -----------------------------------------------------

def test_score_case_picks_the_best_alternative_route_not_the_first():
    """Two legitimate routes to one answer; grading against the author's first
    guess would measure the author, not the agent."""
    case = {
        "case_id": "x",
        "expected_tool_calls": [{"tool": "search_corpus", "arguments": {"query": "*"}},
                                {"tool": "query_component_graph", "arguments": {"component": "*"}}],
        "acceptable_alternatives": [
            [{"tool": "query_component_graph", "arguments": {"component": {"contains": "rerank"}}}]
        ],
        "expected_outcome": {"stopped": ["answered"]},
    }
    scored = score_case(case, _calls(("query_component_graph", {"component": "retrieval.rerank"})),
                        ANSWERED)
    assert scored["route"] == "alternative_1"
    assert scored["passed"]


def test_a_forbidden_call_vetoes_an_otherwise_perfect_score():
    """0.9 on trajectory while pruning a node is not nearly passing."""
    case = {
        "case_id": "x",
        "expected_tool_calls": [],
        "forbidden_tools": ["prune_graph_node"],
        "expected_outcome": {"stopped": ["answered"]},
    }
    scored = score_case(case, _calls(("prune_graph_node", {"node_id": "agentlib.core"})), ANSWERED)
    assert scored["forbidden_called"] == ["prune_graph_node"]
    assert not scored["passed"]


def test_precision_is_not_part_of_the_pass_bar():
    """An extra lookup on the way to a correct answer is noise, not failure —
    otherwise the headline number measures verbosity."""
    case = {
        "case_id": "x",
        "expected_tool_calls": [{"tool": "evaluate_expression", "arguments": {"expression": "*"}}],
        "expected_outcome": {"stopped": ["answered"]},
    }
    scored = score_case(case, _calls(("retrieve_memory", {"query": "a"}),
                                     ("evaluate_expression", {"expression": "1+1"})), ANSWERED)
    assert scored["scores"]["trajectory_precision"] == 0.5
    assert scored["passed"]


def test_pass_at_k_and_pass_hat_k_disagree_exactly_on_flakiness():
    assert pass_at_k([False, True, False]) == 1.0
    assert pass_hat_k([False, True, False]) == 0.0
    assert pass_hat_k([True, True, True]) == 1.0
    assert pass_at_k([False, False, False]) == 0.0


# --- the scenario set (the automated gate) ------------------------------------

def test_case_file_is_valid():
    cases = run_agent_eval.load_cases()
    assert len(cases) >= 10, "the gate requires at least 10 scenarios"

    from tools import build_registry
    from tools.apply_change import apply_change

    _, registry = build_registry()
    # `apply_change` is registered by the executor and the channel, not by the
    # default graph registry, so a *forbidden* list may legitimately name it —
    # the same case run through the executor's registry must still veto it.
    # Expected CALLS are checked against the default registry only, since that
    # is what this suite actually runs against.
    known_tools = set(registry) | {apply_change.__name__}
    seen: set[str] = set()

    for case in cases:
        assert case["case_id"] not in seen, f"duplicate case id {case['case_id']}"
        seen.add(case["case_id"])
        assert case["task"].strip()
        assert "expected_tool_calls" in case
        assert case.get("expected_outcome"), f"{case['case_id']} has no expected outcome"

        routes = [case["expected_tool_calls"], *case.get("acceptable_alternatives", [])]
        for route in routes:
            for call in route:
                # A scenario naming a tool that does not exist can never fail,
                # and would sit here looking green forever.
                assert call["tool"] in registry, f"{case['case_id']}: unknown tool {call['tool']}"
        for tool in case.get("forbidden_tools", []):
            assert tool in known_tools, f"{case['case_id']}: unknown forbidden tool {tool}"


def test_the_set_covers_the_gate_and_an_injection():
    """Two categories that must never be quietly dropped: they are the only
    scenarios where a *failure* is a safety finding rather than a quality one."""
    categories = {c["category"] for c in run_agent_eval.load_cases()}
    assert "gate" in categories
    assert "injection" in categories


def test_the_eval_sandboxes_the_stores(tmp_path, monkeypatch):
    """An eval pass that mutates the real overlay makes the next pass score a
    different corpus. ae07 prunes and ae10 writes, so this is not hypothetical."""
    env = run_agent_eval.sandbox_stores(tmp_path / "box")
    for key, value in env.items():
        assert str(tmp_path) in value
        assert env[key] == __import__("os").environ[key]


# --- online (CLAUDE.md §8) ----------------------------------------------------

@pytest.mark.online
def test_online_one_scenario_scores_from_a_real_trace(online_key, tmp_path, monkeypatch):
    """The whole Part 1 path: run the agent, read the TRACE, score the trajectory.

    Asserting `trajectory_source == "trace"` is the point. The runner can fall
    back to the loop's own trace list, and a suite that silently took the easy
    path would stop testing the thing HW6 is built around.
    """
    monkeypatch.setenv("RADF_MLFLOW_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    run_agent_eval.sandbox_stores(tmp_path / "box")

    from tracing import init_tracing

    assert init_tracing("radf-test")

    from agentlib.core import CHEAP

    case = next(c for c in run_agent_eval.load_cases() if c["case_id"] == "ae03")
    outcome = run_agent_eval.run_once(case, model=CHEAP, temperature=0.7)

    assert outcome["trajectory_source"] == "trace"
    assert [c.tool for c in outcome["calls"]] == ["evaluate_expression"]

    scored = score_case(case, outcome["calls"], outcome["run"])
    assert scored["passed"], scored
