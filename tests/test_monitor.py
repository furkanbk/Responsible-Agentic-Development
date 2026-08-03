"""tests/test_monitor.py — the LLM-as-judge monitor (HW2, T7.5).

Owner: Dias Sarkytbaev. New file.

The judge itself calls a model, so the model is SCRIPTED here (monkeypatching
`monitor.judge.call`, the same trick the planner and smoke suites use). That
keeps the tests deterministic and offline while exercising the REAL judge: its
rationale drop, its in-context check, and its computed labels.

The fixture `tests/fixtures/runs_sample.jsonl` is hand-written in the frozen
run-log shape (TODO §Contracts) — a clean run, a minor violation, a serious
violation, and a run whose broken rule was never assembled. "Start with the
fixture, not with a live run" (TODO, Phase 7b).

Run:  python -m pytest tests/test_monitor.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _online import online_key  # noqa: F401 — pytest fixture, used by name

import monitor.judge as judge_mod
from agentlib.core import Result
from monitor.judge import (
    ADHERENCE,
    GROUNDING,
    judge_run,
    load_rubric,
    problems,
    report,
)

FIXTURE = Path(__file__).parent / "fixtures" / "runs_sample.jsonl"


def runs() -> dict[str, dict]:
    """The fixture, keyed by run_id."""
    out: dict[str, dict] = {}
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rec = json.loads(line)
            out[rec["run_id"]] = rec
    return out


def script(monkeypatch, payload) -> None:
    """Make the judge's model return exactly `payload` (a dict -> JSON, or raw text)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(judge_mod, "call",
                        lambda *a, **k: Result(text=text))


def script_result(monkeypatch, result: Result) -> None:
    monkeypatch.setattr(judge_mod, "call", lambda *a, **k: result)


# --- the four fixture runs ----------------------------------------------------


def test_clean_run_strictly_adheres(monkeypatch):
    script(monkeypatch, {"grounding": "grounded", "violations": []})
    v = judge_run(runs()["r_clean01"])
    assert v.gradeable
    assert v.prompt_adherence == "strictly_adheres"
    assert v.grounding == "grounded"
    assert v.violations == [] and v.assembler_gaps == []
    assert not v.is_problem


def test_minor_violation_is_counted_in_context(monkeypatch):
    # Skipped the R2 second lookup but still answered — outcome unchanged => minor.
    script(monkeypatch, {"grounding": "grounded", "violations": [{
        "rule": "R2", "rule_quote": "Two lookups, never one", "severity": "minor",
        "expected": "call retrieve_decisions as well as query_component_graph",
        "observed": "only query_component_graph was called"}]})
    v = judge_run(runs()["r_minor01"])
    assert v.prompt_adherence == "minor_violation"
    assert len(v.violations) == 1 and v.violations[0]["rule"] == "R2"
    assert v.assembler_gaps == [] and v.dropped == []


def test_serious_violation_is_counted_in_context(monkeypatch):
    # Wrote a file outside the impact set (R7) — a boundary crossed => serious.
    script(monkeypatch, {"grounding": "grounded", "violations": [{
        "rule": "R7", "rule_quote": "only write files the plan listed as impacted",
        "severity": "serious",
        "expected": "write only files in the impact set [Module:tools.decisions]",
        "observed": "apply_change wrote agentlib/loop.py, outside the set"}]})
    v = judge_run(runs()["r_serious01"])
    assert v.prompt_adherence == "serious_violation"
    assert v.is_problem


# --- the headline distinction (T7.3c): ignored vs never-assembled -------------


def test_rule_never_assembled_is_an_assembler_gap_not_a_model_failure(monkeypatch):
    # The model accuses the run of breaking R5 (no decision recorded on a
    # contract change). But r_gap01's assembled.instructions never contained R5,
    # so it is the assembler's fault, not the model's.
    script(monkeypatch, {"grounding": "grounded", "violations": [{
        "rule": "R5", "rule_quote": "Record what would otherwise be re-derived",
        "severity": "serious",
        "expected": "append_decision_record after changing a contract",
        "observed": "no decision was recorded for the signature change"}]})
    v = judge_run(runs()["r_gap01"])
    # NOT blamed on the model:
    assert v.prompt_adherence == "strictly_adheres"
    assert v.violations == []
    # surfaced as an assembler gap instead:
    assert len(v.assembler_gaps) == 1 and v.assembler_gaps[0]["rule"] == "R5"
    assert v.is_problem                      # still worth reporting — just to the right owner


def test_the_same_R5_violation_is_counted_when_the_rule_was_assembled(monkeypatch):
    # Contrast: r_clean01's instructions DO contain R5, so the identical accusation
    # is a real, in-context model failure.
    script(monkeypatch, {"grounding": "grounded", "violations": [{
        "rule": "R5", "rule_quote": "Record what would otherwise be re-derived",
        "severity": "serious",
        "expected": "append_decision_record after a non-obvious choice",
        "observed": "no decision recorded"}]})
    v = judge_run(runs()["r_clean01"])
    assert v.prompt_adherence == "serious_violation"
    assert len(v.violations) == 1 and v.assembler_gaps == []


# --- the rationale guard (T7.3a) ---------------------------------------------


def test_a_verdict_without_rationale_is_dropped(monkeypatch):
    # A violation naming a rule but no expected/observed is unverifiable -> discarded.
    script(monkeypatch, {"grounding": "grounded", "violations": [{
        "rule": "R4", "severity": "serious"}]})
    v = judge_run(runs()["r_serious01"])
    assert v.prompt_adherence == "strictly_adheres"     # the unbacked accusation is gone
    assert v.violations == []
    assert len(v.dropped) == 1


def test_partial_rationale_is_still_dropped(monkeypatch):
    script(monkeypatch, {"grounding": "grounded", "violations": [{
        "rule": "R7", "severity": "serious", "expected": "stay in the impact set"}]})
    v = judge_run(runs()["r_serious01"])
    assert v.violations == [] and len(v.dropped) == 1


# --- grounding is trusted only when backed -----------------------------------


def test_unbacked_grounding_downgrade_is_not_trusted(monkeypatch):
    script(monkeypatch, {"grounding": "ungrounded", "violations": []})
    v = judge_run(runs()["r_clean01"])
    assert v.grounding == "grounded"          # no rationale -> cannot be trusted


def test_backed_grounding_downgrade_stands(monkeypatch):
    script(monkeypatch, {"grounding": "ungrounded",
                         "grounding_rationale": "the answer names a module with no query step",
                         "violations": []})
    v = judge_run(runs()["r_clean01"])
    assert v.grounding == "ungrounded"
    assert v.is_problem


# --- ungradeable, never guessed ----------------------------------------------


def test_unparseable_judge_output_is_ungradeable(monkeypatch):
    script(monkeypatch, "the run looked basically fine to me, no json here")
    v = judge_run(runs()["r_clean01"])
    assert v.gradeable is False
    assert v.prompt_adherence == "ungraded"


def test_truncated_judge_output_is_ungradeable(monkeypatch):
    script_result(monkeypatch, Result(text='{"grounding": "grou', truncated=True))
    v = judge_run(runs()["r_clean01"])
    assert v.gradeable is False


# --- rubric + reporting -------------------------------------------------------


def test_rubric_loads_and_names_the_levels():
    text = load_rubric().lower()
    assert "minor_violation" in text and "serious_violation" in text
    assert "grounded" in text


def test_report_shows_expected_and_observed(monkeypatch):
    script(monkeypatch, {"grounding": "grounded", "violations": [{
        "rule": "R7", "rule_quote": "only write files the plan listed as impacted",
        "severity": "serious",
        "expected": "write only files in the impact set",
        "observed": "wrote agentlib/loop.py outside it"}]})
    text = report([judge_run(runs()["r_serious01"])])
    assert "expected:" in text and "observed:" in text
    assert "R7" in text


def test_problems_surfaces_the_serious_run_not_the_clean_one(monkeypatch):
    script(monkeypatch, {"grounding": "grounded", "violations": []})
    clean = judge_run(runs()["r_clean01"])
    script(monkeypatch, {"grounding": "grounded", "violations": [{
        "rule": "R7", "rule_quote": "only write files the plan listed as impacted",
        "severity": "serious", "expected": "stay in the impact set",
        "observed": "wrote outside it"}]})
    serious = judge_run(runs()["r_serious01"])
    found = problems([clean, serious])
    assert serious in found and clean not in found


# --- online: a real judge model over a fixture run (HW4, T13b / CLAUDE.md §8) --


@pytest.mark.online
def test_online_judge_returns_a_verdict_within_its_contract(online_key):
    """One real model call through the judge, asserted on its CONTRACT.

    Every other test here scripts the model, which proves my reconciliation
    logic but never proves the real model's reply survives `_parse` — a mocked
    judge always returns exactly the JSON the mock was written to return. That
    is the class of bug §8 exists for, so this test asks the live model and
    checks the shape it must always satisfy:

      * the verdict is gradeable (the reply parsed at all),
      * both axes carry one of their NAMED values, never a score,
      * every surviving violation carries its expected/observed rationale
        (T7.3a) — the code drops the unbacked ones, so anything that made it
        this far must be checkable.

    Deliberately not asserted: WHICH verdict comes back. The wording and the
    severity a live judge picks vary run to run; pinning them would make this a
    flake rather than a contract test.
    """
    verdict = judge_run(runs()["r_serious01"])

    assert verdict.gradeable, f"live judge returned no parseable verdict: {verdict.notes}"
    assert verdict.prompt_adherence in ADHERENCE
    assert verdict.grounding in GROUNDING
    for violation in verdict.violations:
        assert violation.get("expected"), f"kept violation with no expected: {violation}"
        assert violation.get("observed"), f"kept violation with no observed: {violation}"
