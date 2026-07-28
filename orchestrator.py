"""orchestrator.py — run a change request through planner then executor.

Owner: Berat Furkan Kocak (HW2, T6.4).

**Plain Python. No model call.** A coordinator whose only job is routing does
not earn one: every decision it makes is a branch on an enum, and a branch on an
enum written in Python is testable, free, and cannot be talked out of its
decision by text in its context. Adding a third LLM here would make the system
more expensive, slower, and less predictable, in exchange for nothing.

The two agents never call each other. The orchestrator calls each in turn and
passes work through:

    planner ──> AgentResult ──> [orchestrator branches on .status]
                                     │
                                     ├─ ok           write plan to run_scratch
                                     │               and call the executor
                                     ├─ needs_input  stop, surface the questions
                                     ├─ blocked      stop, report the constraint
                                     └─ failed       stop, report the fault

Note what the branch does NOT do: read either agent's prose. `notes` is for
humans and the run log. The moment flow control depends on a sentence, the
coordination breaks the first time a model rephrases itself.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Callable, Optional

from dotenv import load_dotenv

from agentlib.approval import preview_args
from agentlib.core import CHEAP, STRONG
from agentlib.runlog import RunLog
from agentlib.session import session_scope
from agents.envelope import AgentResult
from agents.executor import run_executor
from agents.planner import run_planner
from overlay import db as overlay_db

PLAN_KEY = "plan"


def approve_via_input(name: str, args: dict) -> bool:
    """Human gate for irreversible tools. Anything but an explicit 'y' declines.

    Renders through `agentlib.approval.preview_args` so the terminal gate and
    the channel gate describe the same action — and, more to the point, surface
    the same constraints. The two drifting is how one of them ends up asking a
    human to approve a write that a recorded decision forbids, without saying so.
    """
    print(f"\n    [GATE] {preview_args(name, args)}")
    return input("    Approve this irreversible action? [y/N] ").strip().lower() == "y"


def run_change_request(
    request: str,
    *,
    user_id: str,
    thread_id: str = "default",
    component_hint: str = "",
    approve: Optional[Callable[[str, dict], bool]] = None,
    model: str = CHEAP,
    verbose: bool = True,
    planner: Callable[..., AgentResult] = run_planner,
    executor: Callable[..., AgentResult] = run_executor,
) -> dict[str, Any]:
    """Plan a change, then implement it. One run record covers both agents.

    `planner` and `executor` are injectable so the pipeline can be tested
    without a model — the orchestrator's job is the branching, and the branching
    is what the tests should exercise.

    Returns:
        {"status", "plan", "changes", "run_id", "planner", "executor", "notes"}
    """
    with session_scope(user_id, thread_id) as session:
        run_log = RunLog(
            agent="orchestrator", user_id=session.user_id,
            thread_id=session.thread_id, request=request,
        )
        conn = overlay_db.connect()
        try:
            run_id = overlay_db.start_run(
                conn, user_id=session.user_id, thread_id=session.thread_id,
                agent="orchestrator", request=request,
            )
            run_log.run_id = run_id

            # --- stage 1: plan -----------------------------------------------
            if verbose:
                print(f"\n[PLANNER] {session} — {request!r}")
            plan_env = planner(
                request, component_hint=component_hint, model=model,
                verbose=verbose, run_log=run_log,
            )
            run_log.record_envelope("planner", plan_env.to_dict())

            if not plan_env.actionable:
                # One branch per status, and none of them read prose.
                if verbose:
                    print(f"[PLANNER] {plan_env.status}: {plan_env.notes}")
                return _finish(run_log, conn, run_id, plan_env, None, verbose)

            plan = plan_env.result
            overlay_db.scratch_write(
                conn, run_id=run_id, agent="planner", step=1,
                key=PLAN_KEY, value=plan,
            )
            if verbose:
                print(f"[PLANNER] ok — {len(plan.get('impacted') or [])} component(s) "
                      f"impacted, {len(plan.get('steps') or [])} step(s)")
        finally:
            conn.close()

        # --- stage 2: execute -------------------------------------------------
        # The plan is handed over through the scratch table, not as an argument.
        if verbose:
            print("\n[EXECUTOR] implementing")
        exec_env = executor(
            request, run_id=run_id, plan_key=PLAN_KEY, approve=approve,
            model=model, verbose=verbose, run_log=run_log,
        )
        run_log.record_envelope("executor", exec_env.to_dict())

        conn = overlay_db.connect()
        try:
            return _finish(run_log, conn, run_id, plan_env, exec_env, verbose)
        finally:
            conn.close()


def _finish(
    run_log: RunLog,
    conn: Any,
    run_id: str,
    plan_env: AgentResult,
    exec_env: Optional[AgentResult],
    verbose: bool,
) -> dict[str, Any]:
    """Close the run: capture the scratch history, flush the log, report."""
    final = exec_env or plan_env
    run_log.scratch = overlay_db.scratch_dump(conn, run_id)
    run_log.stopped = final.status
    run_log.answer = final.notes
    run_log.flush()
    overlay_db.finish_run(conn, run_id, final.status)

    changes = (exec_env.result.get("changes") if exec_env else []) or []
    if verbose:
        print(f"\n{'=' * 60}\nstatus : {final.status}")
        print(f"changes: {len(changes)} file(s)")
        print(f"notes  : {final.notes}")
        print(f"run_id : {run_id}")

    return {
        "status": final.status,
        "plan": plan_env.result if plan_env.status == "ok" else None,
        "changes": changes,
        "run_id": run_id,
        "planner": plan_env.to_dict(),
        "executor": exec_env.to_dict() if exec_env else None,
        "notes": final.notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RADF HW2 — planner + executor over a change request."
    )
    parser.add_argument("request", help="the change to plan and implement")
    parser.add_argument("--user", default="anonymous",
                        help="acting user id (scopes memory, attributes writes)")
    parser.add_argument("--thread", default="default")
    parser.add_argument("--component", default="",
                        help="optional starting component for the impact walk")
    parser.add_argument("--model", choices=["cheap", "strong"], default="cheap")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        result = run_change_request(
            args.request,
            user_id=args.user,
            thread_id=args.thread,
            component_hint=args.component,
            approve=approve_via_input,
            model=STRONG if args.model == "strong" else CHEAP,
            verbose=not args.quiet,
        )
    except NotImplementedError as exc:
        print(f"\n[stub] reached an unimplemented agent: {exc}", file=sys.stderr)
        return 2

    return 0 if result["status"] in ("ok", "needs_input", "blocked") else 1


if __name__ == "__main__":
    raise SystemExit(main())
