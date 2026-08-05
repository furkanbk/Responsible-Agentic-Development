"""agents.executor — carry out a plan someone else made.

Owner: Berat Furkan Kocak (HW2, T6.3).

The executor's boundaries are written in `agents/executor_brief.md` and pushed
into its instructions every run, the same way operating rules are pushed into
the main agent's. Keeping them in a file rather than in this module is the
point: the brief is a delegation a human can amend in seconds without touching
Python.

## Why it reads the plan from shared scratch

The orchestrator has the plan in a local variable and could pass it as an
argument. It writes it to `run_scratch` instead, and the executor reads it back.

That is deliberately the harder thing to debug, because it is the thing HW2
asks us to reason about. A shared store is a channel with **no call site**:
nothing in this module's signature says it depends on what the planner produced,
so a change in the planner can alter this agent's behaviour with no edge between
them in any call graph. Grep will not find it.

What makes it survivable here:

  * writes are **append-only** — the earlier value is still there when you go
    looking, instead of having been overwritten by the thing you are debugging;
  * every read is **logged with the `seq` it observed**, so "which version of
    the plan did the executor actually act on" is a fact in the log rather than
    a reconstruction;
  * a **missed** read is logged too. "The executor looked for the plan and found
    nothing" and "the executor never looked" are different bugs that otherwise
    produce identical traces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from agentlib.context import assemble
from agentlib.loop import run_agent
from agentlib.schemas import schema_for
from agentlib.session import current_session, impact_scope
from agents.envelope import AgentResult, validate_plan
from overlay import db as overlay_db
from tools.apply_change import apply_change
from tools.decisions import retrieve_decisions
from tools.graph_query import query_component_graph
from tools.read_source import read_source_file

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BRIEF_PATH = _REPO_ROOT / "agents" / "executor_brief.md"

# Narrow by construction. The executor cannot scan, prune, or write memory —
# not because it is told not to, but because those tools are not in its registry
# (executor_brief.md explains each omission).
EXECUTOR_TOOLS: list[Callable] = [
    read_source_file,
    query_component_graph,
    retrieve_decisions,
    apply_change,
]

MAX_STEPS = 8


def load_brief() -> str:
    """The delegation brief, pushed every run."""
    if not _BRIEF_PATH.exists():
        # A missing brief means an executor with tools and no boundary. Refuse
        # loudly rather than run an unbounded agent.
        raise FileNotFoundError(
            f"executor brief missing at {_BRIEF_PATH} — the executor must not "
            "run without its delegation boundary"
        )
    return _BRIEF_PATH.read_text(encoding="utf-8").strip()


def build_executor_registry() -> tuple[list[dict[str, Any]], dict[str, Callable]]:
    """(schemas, registry) for the executor's narrow toolset."""
    return ([schema_for(fn) for fn in EXECUTOR_TOOLS],
            {fn.__name__: fn for fn in EXECUTOR_TOOLS})


def _render_plan(plan: dict) -> str:
    """The plan as text the executor reads. Steps last — they are the action."""
    lines = ["# The plan you are implementing", ""]
    lines.append("Impacted components (the ONLY files you may write):")
    for uid in plan.get("impacted") or ["(none)"]:
        lines.append(f"  - {uid}")
    if plan.get("constraints"):
        lines.append("")
        lines.append("Decision ids this plan honours — call retrieve_decisions "
                     "to read them before editing:")
        for did in plan["constraints"]:
            lines.append(f"  - {did}")
    if plan.get("open_questions"):
        lines.append("")
        lines.append("OPEN QUESTIONS — these are unresolved. You may not act "
                     "alone while any remain:")
        for q in plan["open_questions"]:
            lines.append(f"  - {q}")
    lines.append("")
    lines.append("Steps:")
    for i, step in enumerate(plan.get("steps") or [], 1):
        lines.append(f"  {i}. {step.get('path')} — {step.get('intent', '')}")
    return "\n".join(lines)


def run_executor(
    request: str,
    *,
    run_id: str,
    plan_key: str = "plan",
    approve: Optional[Callable[[str, dict], bool]] = None,
    model: Optional[str] = None,
    max_steps: int = MAX_STEPS,
    verbose: bool = True,
    run_log: Any = None,
) -> AgentResult:
    """Read the plan from shared scratch and carry it out.

    The plan is NOT a parameter — see the module docstring. `run_id` and
    `plan_key` are how this agent finds what the planner left for it.

    Returns an `AgentResult`:
      ok           the steps were carried out (possibly with nothing to write)
      needs_input  open questions remain, or the plan is unusable
      blocked      a constraint or the impact set stopped it
      failed       the loop stalled, hit its cap, or a tool errored
    """
    session = current_session()
    conn = overlay_db.connect()
    try:
        step_no = len(run_log.steps) if run_log is not None else 0
        plan = overlay_db.scratch_read(
            conn, run_id=run_id, agent="executor", step=step_no, key=plan_key
        )
        scratch = overlay_db.scratch_dump(conn, run_id)
    finally:
        conn.close()

    if run_log is not None:
        # The read set is the causal chain. Record it even when the read missed.
        run_log.scratch = scratch

    # The planner never wrote, or wrote under a different key. Distinguishable
    # from "wrote an empty plan" only because the miss was logged.
    if plan is None:
        return AgentResult.failed(
            f"no plan at scratch key {plan_key!r} for run {run_id} — the planner "
            "did not write one, or wrote it under another key",
            agent="executor",
        )

    problems = validate_plan(plan)
    if problems:
        return AgentResult.failed(
            "plan is malformed: " + "; ".join(problems),
            agent="executor", result={"plan": plan},
        )

    if plan.get("open_questions"):
        # The brief says so, and the code enforces it. A boundary that only
        # exists in the prompt is a suggestion.
        return AgentResult.needs_input(
            list(plan["open_questions"]), agent="executor", result={"plan": plan}
        )

    if not plan.get("steps"):
        return AgentResult.ok(
            {"plan": plan, "changes": []}, agent="executor",
            notes="plan had no steps — nothing to implement",
        )

    schemas, registry = build_executor_registry()
    context = assemble(
        base_system=load_brief(),
        query=request,
        impact=plan.get("impacted") or [],
        session=session,
        # The plan already carries its constraints, and the executor is told to
        # fetch them by id. Pulling free-form memory here would put the user's
        # personal preferences into an implementer's context, where they are
        # noise at best.
        include_memory=False,
        include_decisions=False,
    )

    task = f"{_render_plan(plan)}\n\n---\n\nThe original request was:\n{request}"

    # The impact set is ambient for the whole run: apply_change reads it from
    # here, so the model cannot widen its own write permission.
    with impact_scope(plan.get("impacted") or []):
        result = run_agent(
            task,
            schemas,
            registry,
            approve=approve,
            model=model or _default_model(),
            max_steps=max_steps,
            verbose=verbose,
            context=context,
            run_log=None,  # the orchestrator owns the single run record
        )

    return _envelope_from_loop(result, plan, run_log)


def _default_model() -> str:
    from agentlib.core import CHEAP
    return CHEAP


def _envelope_from_loop(result: dict, plan: dict, run_log: Any) -> AgentResult:
    """Map the loop's stopping condition onto the envelope's statuses.

    This mapping is the whole reason the envelope exists: the orchestrator
    branches on `status`, and `status` has to mean something stable regardless
    of which of five ways the loop happened to stop.
    """
    changes = [
        event["output"] for event in result["trace"]
        if event["tool"] == "apply_change" and event["branch"] == "ok"
    ]
    if run_log is not None:
        for change in changes:
            run_log.record_change(change)

    stopped = result["stopped"]
    payload = {"plan": plan, "changes": changes, "trace": result["trace"]}

    # A write the human refused. Not an error — the gate worked.
    declined = [e for e in result["trace"] if e["branch"] == "declined"]
    if stopped == "declined" or (declined and not changes):
        return AgentResult.blocked(
            "a human declined the file write", agent="executor", result=payload
        )

    if stopped in ("max_steps", "stalled", "truncated"):
        return AgentResult.failed(
            f"executor loop stopped as {stopped}", agent="executor", result=payload
        )

    # Confinement refusals are the escalate case from the brief, not a defect.
    blocked_writes = [
        e for e in result["trace"]
        if e["tool"] == "apply_change" and isinstance(e["output"], dict)
        and e["output"].get("error") in ("outside_impact_set", "path_outside_scope")
    ]
    if blocked_writes and not changes:
        return AgentResult.blocked(
            "the change needs a file outside the plan's impact set — the impact "
            "analysis was incomplete",
            agent="executor", result=payload,
        )

    # Nothing was written. For THIS agent that is never `ok`: the executor exists
    # to carry out a plan, so a run that changes no file either refused or gave
    # up, and `ok` is the one status that tells the caller neither.
    #
    # The case that exposed it is the brief's own escalate rule — "a recorded
    # decision in `constraints` forbids the change outright" -> `blocked`. That
    # refusal arrives as an ANSWER with no tool call, so every branch above
    # misses it and it was landing as `ok` with zero changes, one field away from
    # a caller concluding the change went in.
    #
    # Decided on the trace, never on the answer's prose — flow control must not
    # depend on a sentence (#29) — and "wrote no files" is a fact about the
    # trace. WHICH refusal it was stays in `notes`, for the human to read.
    if not changes:
        return AgentResult.blocked(
            result.get("answer")
            or "the executor wrote no files for a plan that asked for writes",
            agent="executor", result=payload,
        )

    return AgentResult.ok(
        payload, agent="executor",
        notes=result.get("answer") or f"{len(changes)} file(s) written",
    )
