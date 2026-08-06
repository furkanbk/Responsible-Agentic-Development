"""eval.run_agent_eval — the agent eval suite, three runs per scenario (T15.8).

Owner: Berat Furkan Kocak (Phase 15B, Part 1).

    python -m eval.run_agent_eval                  # all cases, 3 runs each
    python -m eval.run_agent_eval --case ae07      # one scenario
    python -m eval.run_agent_eval --json out.json  # raw results too

**Temperature is raised on purpose.** Every notebook in the course pins
`temperature=0.0`, and three runs at 0.0 are the same run three times: pass@3 and
pass^3 would be identical by construction and requirement 6 — name a scenario
that passed sometimes and failed other times — would be unsatisfiable. This suite
runs at `EVAL_TEMPERATURE` (0.7), stated in the report header. Nothing else in
the repo changes: `run_agent`'s default is still "send no temperature at all"
(decision #92).

**The trajectory comes from the trace, not from the loop's return value.** The
whole ordering of HW6 is built on that: `run_agent` already returns a `trace`
list, and scoring it would have been easier — and would have measured a
hand-rolled log rather than the thing production actually emits. The loop's list
is kept only as a fallback for a machine with no MLflow, and every run records
which source it used, because a suite that silently degrades to the easy path is
a suite that stops testing what it claims to.

**The stores are sandboxed.** `ae07` prunes a graph node and `ae10` writes a
memory. Run against the real stores, an eval pass would mutate the developer's
overlay, and the *second* pass would then score a different corpus. So the runner
copies `store/` into a temp directory and points the env at the copy — the same
redirection `tests/conftest.py` does per test, for the same reason. Postgres is
not copied: retrieval is read-only here, and it is a shared service rather than a
file (that trade is already made in `conftest.configured_dsn`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional, Sequence

from .agent_metrics import METRIC_NAMES, aggregate, pass_at_k, pass_hat_k, score_case

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = Path(__file__).resolve().parent / "agent_cases.json"

# Stated in the report header. See the module docstring for why it is not 0.0.
EVAL_TEMPERATURE = 0.7
RUNS_PER_CASE = 3
MAX_STEPS = 8

# The eval's identity. A real user id, because visibility is filtered in the
# query (#24) and an eval running as nobody would see a different corpus than
# the one people actually query. Deliberately not "berat": the private rows are
# not part of the fixture.
EVAL_USER = "eval"


# --- store sandbox ------------------------------------------------------------

def sandbox_stores(box: Path) -> dict[str, str]:
    """Copy the real stores into `box` and point the env at the copy.

    Returns the env mapping applied, for the report. `RADF_RETRIEVAL_CACHE` is
    NOT redirected — the embedding/rerank cache is content-hashed and derived
    (#63), so sharing it across runs is free and saves an API call per query.
    """
    source = _REPO_ROOT / "store"
    box.mkdir(parents=True, exist_ok=True)
    for name in ("knowledge_graph.json", "radf.db", "memory.json"):
        src = source / name
        if src.exists():
            shutil.copy2(src, box / name)

    env = {
        "RADF_GRAPH_PATH": str(box / "knowledge_graph.json"),
        "RADF_DB_PATH": str(box / "radf.db"),
        "RADF_MEMORY_PATH": str(box / "memory.json"),
        "RADF_RUNS_DIR": str(box / "runs"),
    }
    os.environ.update(env)
    return env


# --- one run ------------------------------------------------------------------

def _approver(case: dict[str, Any]):
    """The gate for this case. Denies unless the case explicitly allows.

    Deny-by-default is the same fail-safe `main.py::approve_via_input` uses:
    anything but an explicit yes declines. An eval that approved destructive
    calls by default would be a suite that deletes its own fixture.
    """
    allow = (case.get("approve") or "deny") == "allow"
    return lambda name, args: allow


def run_once(case: dict[str, Any], *, model: str, temperature: float,
             verbose: bool = False) -> dict[str, Any]:
    """Run one scenario once, traced, and return what the scorers need."""
    from agentlib.context import assemble
    from agentlib.loop import DEFAULT_SYSTEM, run_agent
    from agentlib.runlog import RunLog
    from agentlib.session import session_scope
    from tools import build_registry
    from tracing import (eval_case_scope, get_trace, request_origin_scope,
                         tool_branches, tool_calls_from_trace, tracing_enabled)
    from tracing.trajectory import ToolCall

    schemas, registry = build_registry()

    # `batch` separates every eval run from interactive traffic; `eval_case_id`
    # is the join back to the case that produced the trace (T15.5, contract #14).
    with request_origin_scope("batch"), eval_case_scope(case["case_id"]), \
            session_scope(EVAL_USER, "agent-eval") as session:
        context = assemble(base_system=DEFAULT_SYSTEM, query=case["task"],
                           impact=[], session=session)
        run_log = RunLog(agent="agent-eval", user_id=session.user_id,
                         thread_id=session.thread_id, request=case["task"])
        result = run_agent(
            case["task"], schemas, registry,
            approve=_approver(case), model=model, max_steps=MAX_STEPS,
            verbose=verbose, context=context, run_log=run_log,
            temperature=temperature,
        )

    trace = get_trace(run_log.trace_id) if (tracing_enabled() and run_log.trace_id) else None
    if trace is not None:
        calls = tool_calls_from_trace(trace)
        branches = tool_branches(trace)
        source = "trace"
    else:
        # Fallback only. Recorded, never silent — see the module docstring.
        calls = [ToolCall(tool=ev["tool"], arguments=dict(ev.get("args") or {}))
                 for ev in result.get("trace") or []]
        branches = [{"tool": ev["tool"], "branch": ev.get("branch")}
                    for ev in result.get("trace") or []]
        source = "loop"

    return {
        "calls": calls,
        "run": {
            "stopped": result["stopped"],
            "answer": result["answer"],
            "steps": result["steps"],
            "branches": branches,
        },
        "trace_id": run_log.trace_id,
        "run_id": run_log.run_id,
        "trajectory_source": source,
    }


# --- the suite ----------------------------------------------------------------

def evaluate(cases: Sequence[dict[str, Any]], *, runs: int = RUNS_PER_CASE,
             model: str, temperature: float = EVAL_TEMPERATURE,
             verbose: bool = False) -> dict[str, Any]:
    """Every case, `runs` times each. The report is built from what this returns."""
    per_case: list[dict[str, Any]] = []

    for case in cases:
        attempts: list[dict[str, Any]] = []
        for attempt in range(runs):
            outcome = run_once(case, model=model, temperature=temperature, verbose=verbose)
            scored = score_case(case, outcome["calls"], outcome["run"])
            scored.update({
                "attempt": attempt + 1,
                "trace_id": outcome["trace_id"],
                "run_id": outcome["run_id"],
                "trajectory_source": outcome["trajectory_source"],
                "stopped": outcome["run"]["stopped"],
                "answer": (outcome["run"]["answer"] or "")[:400],
            })
            attempts.append(scored)
            if verbose:
                mark = "PASS" if scored["passed"] else "FAIL"
                print(f"  [{case['case_id']} #{attempt + 1}] {mark} "
                      f"{[c['tool'] for c in scored['calls']]}")

        passes = [a["passed"] for a in attempts]
        per_case.append({
            "case_id": case["case_id"],
            "category": case.get("category", ""),
            "task": case["task"],
            "attempts": attempts,
            "pass_count": sum(passes),
            "pass_at_k": pass_at_k(passes),
            "pass_hat_k": pass_hat_k(passes),
            "flaky": 0 < sum(passes) < len(passes),
            "metrics": aggregate([a["scores"] for a in attempts]),
        })

    return {
        "model": model,
        "temperature": temperature,
        "runs_per_case": runs,
        "cases": per_case,
        "overall": {
            **aggregate([a["scores"] for c in per_case for a in c["attempts"]]),
            "pass_at_k": statistics.fmean([c["pass_at_k"] for c in per_case]) if per_case else None,
            "pass_hat_k": statistics.fmean([c["pass_hat_k"] for c in per_case]) if per_case else None,
        },
        "flaky_cases": [c["case_id"] for c in per_case if c["flaky"]],
        "untraced_runs": sum(1 for c in per_case for a in c["attempts"]
                             if a["trajectory_source"] != "trace"),
    }


# --- report -------------------------------------------------------------------

def _fmt(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.2f}"


def _short(name: str) -> str:
    return {
        "tool_selection_accuracy": "select",
        "tool_parameter_accuracy": "params",
        "goal_completion": "goal",
        "trajectory_precision": "traj P",
        "trajectory_recall": "traj R",
    }[name]


def format_report(results: dict[str, Any]) -> str:
    """Markdown ready to paste into README § HW6 Part 1."""
    lines = [
        f"Model `{results['model']}` · **temperature {results['temperature']}** · "
        f"{results['runs_per_case']} runs per scenario · {len(results['cases'])} scenarios.",
        "",
        "| case | category | pass@3 | pass^3 | " + " | ".join(_short(m) for m in METRIC_NAMES) + " |",
        "|---|---|---|---|" + "|".join(["---"] * len(METRIC_NAMES)) + "|",
    ]
    for case in results["cases"]:
        cells = " | ".join(_fmt(case["metrics"][m]) for m in METRIC_NAMES)
        lines.append(
            f"| {case['case_id']} | {case['category']} | "
            f"{int(case['pass_at_k'])} | {int(case['pass_hat_k'])} "
            f"({case['pass_count']}/{results['runs_per_case']}) | {cells} |"
        )
    overall = results["overall"]
    cells = " | ".join(_fmt(overall[m]) for m in METRIC_NAMES)
    lines.append(f"| **overall** | | **{_fmt(overall['pass_at_k'])}** | "
                 f"**{_fmt(overall['pass_hat_k'])}** | {cells} |")

    lines.append("")
    if results["flaky_cases"]:
        lines.append(f"**Flaky scenarios** (passed some runs, failed others): "
                     f"{', '.join(results['flaky_cases'])}")
        for case in results["cases"]:
            if not case["flaky"]:
                continue
            lines.append("")
            lines.append(f"*{case['case_id']}* — what varied across the three runs:")
            for attempt in case["attempts"]:
                mark = "pass" if attempt["passed"] else "FAIL"
                calls = " → ".join(f"{c['tool']}({json.dumps(c['arguments'])[:60]})"
                                   for c in attempt["calls"]) or "(no tool call)"
                lines.append(f"  - run {attempt['attempt']} [{mark}, stopped="
                             f"{attempt['stopped']}]: {calls}")
    else:
        lines.append("**No flaky scenario in this pass.** Every case was 3-for-3 or 0-for-3, "
                     "which usually means the scenarios are too easy or the tolerance is too "
                     "loose — worth re-reading before believing the table.")

    if results["untraced_runs"]:
        lines.append("")
        lines.append(f"> {results['untraced_runs']} run(s) scored from the loop's own trace list "
                     f"because no MLflow trace was available. Those rows do not demonstrate the "
                     f"trace → trajectory path.")
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------

def load_cases(path: Optional[Path] = None) -> list[dict[str, Any]]:
    data = json.loads((path or DEFAULT_CASES).read_text(encoding="utf-8"))
    return list(data["cases"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="HW6 Part 1 — agent evaluation")
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument("--case", action="append", default=None,
                        help="run only these case ids (repeatable)")
    parser.add_argument("--runs", type=int, default=RUNS_PER_CASE)
    parser.add_argument("--temperature", type=float, default=EVAL_TEMPERATURE)
    parser.add_argument("--model", choices=["cheap", "strong"], default="cheap")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")

    box = Path(tempfile.mkdtemp(prefix="radf-agent-eval-"))
    sandbox_stores(box)

    from agentlib.core import CHEAP, STRONG
    from tracing import init_tracing

    if not init_tracing():
        print("mlflow unavailable — trajectories will fall back to the loop's own "
              "trace list, which is NOT what Part 1 is demonstrating.", file=sys.stderr)

    cases = load_cases(args.cases)
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c["case_id"] in wanted]
        if not cases:
            print(f"no case matched {sorted(wanted)}", file=sys.stderr)
            return 2

    results = evaluate(
        cases, runs=args.runs, model=STRONG if args.model == "strong" else CHEAP,
        temperature=args.temperature, verbose=not args.quiet,
    )
    results["store_sandbox"] = str(box)

    print()
    print(format_report(results))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"\nraw results -> {args.json}", file=sys.stderr)
    print(f"sandboxed stores: {box}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
