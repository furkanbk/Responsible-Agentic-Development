"""agents.envelope — the object agents hand each other.

Owner: Berat Furkan Kocak (HW2, T6.1). **This is a frozen contract** — the
planner (Alejandro), `apply_change` (Alejandro) and the monitor (Dias) all build
against it. Changing a field name is a team decision, not a refactor.

## Why an envelope and not prose

Two agents that read each other's paragraphs are two agents whose coordination
you cannot test. "Did the planner say it was blocked?" becomes a substring
search that works until the model rephrases. With an envelope, the orchestrator
branches on `status` and `needs_approval`, both of which are enums it can
exhaust — and a new status the orchestrator does not know about is a loud
KeyError rather than a silently mis-routed run.

The rule downstream: **branch on fields, read prose only to show a human.**
`notes` exists for the human. Nothing may control flow on it.

## The statuses, and where the lines fall

    ok           the agent did its job. `result` is valid and complete.
    needs_input  it cannot proceed without a human answer. NOT a failure — the
                 planner raising an open question is the system working.
    blocked      it could proceed technically, but a constraint says it must not
                 (a decision forbids the change, the plan wants a file outside
                 the impact set). The distinction from `failed` matters: blocked
                 is a correct refusal, failed is a defect.
    failed       something broke. A tool errored, the model stalled, the loop hit
                 its cap.

`needs_approval` is orthogonal to all four. An `ok` result can still require a
human to approve the side effect it wants; that is the gate, and the gate is the
loop's, not the agent's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Status = Literal["ok", "needs_input", "blocked", "failed"]

# Keys every plan dict carries. Frozen alongside this file (TODO.md §Contracts).
PLAN_KEYS = ("impacted", "constraints", "rules_applied", "steps", "open_questions")


@dataclass
class AgentResult:
    """One agent's output, in the shape the next stage branches on.

    Fields:
      status          see the module docstring. The orchestrator's switch.
      result          the payload. For the planner, a plan dict; for the
                      executor, a summary of what it changed.
      needs_approval  a human must approve a side effect before it happens.
      notes           for humans and the run log. **Never branch on this.**
      agent           who produced it, for the trace.
    """

    status: Status
    result: dict[str, Any] = field(default_factory=dict)
    needs_approval: bool = False
    notes: str = ""
    agent: str = ""

    # --- constructors, so callers do not hand-build a status string ----------

    @classmethod
    def ok(cls, result: dict, *, agent: str = "", needs_approval: bool = False,
           notes: str = "") -> "AgentResult":
        return cls("ok", result, needs_approval, notes, agent)

    @classmethod
    def needs_input(cls, questions: list[str], *, agent: str = "",
                    result: Optional[dict] = None) -> "AgentResult":
        payload = dict(result or {})
        payload["open_questions"] = questions
        return cls("needs_input", payload, False,
                   "; ".join(questions), agent)

    @classmethod
    def blocked(cls, reason: str, *, agent: str = "",
                result: Optional[dict] = None) -> "AgentResult":
        """A correct refusal — a constraint says stop. Not a defect."""
        return cls("blocked", dict(result or {}), False, reason, agent)

    @classmethod
    def failed(cls, reason: str, *, agent: str = "",
               result: Optional[dict] = None) -> "AgentResult":
        return cls("failed", dict(result or {}), False, reason, agent)

    # --- predicates the orchestrator uses ------------------------------------

    @property
    def actionable(self) -> bool:
        """True iff the next stage may act on this without a human first."""
        return self.status == "ok" and not self.needs_approval

    def to_dict(self) -> dict[str, Any]:
        """The form that goes into the run log and the shared scratch."""
        return {
            "agent": self.agent,
            "status": self.status,
            "result": self.result,
            "needs_approval": self.needs_approval,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentResult":
        """Rebuild from scratch/log storage. Unknown status is a hard failure —
        silently coercing it would route a run down the wrong branch."""
        status = data.get("status")
        if status not in ("ok", "needs_input", "blocked", "failed"):
            raise ValueError(f"unknown agent status: {status!r}")
        return cls(
            status=status,
            result=data.get("result") or {},
            needs_approval=bool(data.get("needs_approval")),
            notes=data.get("notes") or "",
            agent=data.get("agent") or "",
        )


# --- the plan dict ------------------------------------------------------------

Plan = dict[str, Any]


def empty_plan() -> Plan:
    """A plan with every frozen key present and empty.

    Build on this rather than assembling a dict literal: a missing key and an
    empty one mean different things downstream, and `apply_change` treats a
    missing `impacted` as "nothing is in scope", not as "everything is".
    """
    return {
        "impacted": [],       # symbol_uids, already through resolve_uid
        "constraints": [],    # decision_ids this plan honours
        "rules_applied": [],  # rule file paths that bound to the impact set
        "steps": [],          # [{"path": ..., "intent": ...}]
        "open_questions": [], # non-empty => the executor must ask, not act
    }


def validate_plan(plan: Any) -> list[str]:
    """Structural problems with a plan dict. Empty list means well-formed.

    Deliberately structural only — whether the impact set is *correct* is the
    planner's job and the monitor's question. This just guarantees that
    downstream code can index the thing without guessing.
    """
    if not isinstance(plan, dict):
        return ["plan must be an object"]

    problems: list[str] = []
    for key in PLAN_KEYS:
        if key not in plan:
            problems.append(f"missing required key: {key}")
        elif not isinstance(plan[key], list):
            problems.append(f"{key} must be a list")

    for uid in plan.get("impacted", []) if isinstance(plan.get("impacted"), list) else []:
        if not isinstance(uid, str) or ":" not in uid:
            problems.append(
                f"impacted entry {uid!r} is not a symbol_uid — pass it through "
                "overlay.uid.resolve_uid first"
            )

    steps = plan.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or "path" not in step:
                problems.append(f"step {step!r} must be an object with a 'path'")

    return problems
