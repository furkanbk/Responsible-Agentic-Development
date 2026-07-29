"""agents.admin — the privileged subagent, and the two locks on its door.

Owner: Dias Sarkytbaev (HW3, T11.3).

Every other agent in the system reads. This one writes across the durable layer:
it can record decisions, re-point an orphaned `symbol_uid`, promote an inferred
memory, prune a dead node. That authority is fenced two ways that are enforced
here in code, not asked for in a prompt:

  1. **Identity AND confirmation (decision #48).** The sender must resolve to an
     admin id (`channel.identity.admins()`) AND give an explicit in-channel
     confirmation for this action. An allowlist alone makes every message from an
     admin a privileged one, including the ones they did not mean that way. Both,
     or the subagent does not run.

  2. **A registry narrow by construction (decision #48).** `ADMIN_TOOLS` is an
     explicit list, so `build_admin_registry()` contains exactly those tools.
     It is never `build_registry()` with a filter over it: a filter is one bug
     away from being the full registry; a list is not.

Write scope is still the ambient impact set (decision #25): empty denies every
write, so an admin run touches only the components granted to it for that run.
The human approval gate on `apply_change` / `prune_graph_node` is the loop's and
is not removed for admins. The written boundary is `rules/ADMIN_BOUNDARY.md`,
pushed every run the way the executor's brief is (decision #31).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from agentlib.context import assemble
from agentlib.core import STRONG
from agentlib.loop import run_agent
from agentlib.schemas import schema_for
from agentlib.session import current_impact_set, impact_scope, session_scope
from agents.envelope import AgentResult
from channel.identity import Identity
from overlay import memory as overlay_memory
from tools.apply_change import apply_change
from tools.decisions import append_decision_record, retrieve_decisions
from tools.graph_query import query_component_graph
from tools.graph_write import prune_graph_node
from tools.memory_tools import retrieve_memory

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BRIEF_PATH = _REPO_ROOT / "rules" / "ADMIN_BOUNDARY.md"

MAX_STEPS = 8


# --- a thin admin-owned tool: nothing in the system promotes memory yet -------

def promote_memory(memory_id: str) -> dict[str, Any]:
    """Promote one inferred `proposed` memory to `accepted`, by its id.

    Use this only on explicit confirmation that an inferred preference is real
    (decision #28 defines the promotion; this is the tool that performs it).
    An `accepted` memory is assembled into future runs by default, so promoting
    the wrong one changes behaviour silently.

    When NOT to call: not to CREATE memory (that is `save_memory`), and not on a
    memory that is already `accepted`. Only to lift a `proposed` one after a
    human confirmed it.

    Returns the promoted record, or `{"error": "memory_not_found", ...}` — a
    structured error, never a raise.
    """
    record = overlay_memory.promote_memory(memory_id)
    if record is None:
        return {"error": "memory_not_found", "memory_id": memory_id}
    return record


# Narrow BY CONSTRUCTION. Reads the ordinary agent has, plus the four writes the
# ordinary agent is denied. This list IS the privilege boundary.
ADMIN_TOOLS: list[Callable] = [
    # reads
    query_component_graph,
    retrieve_decisions,
    retrieve_memory,
    # writes (each still subject to the ambient impact set and the loop's gate)
    append_decision_record,
    apply_change,
    prune_graph_node,
    promote_memory,
]


def load_admin_brief() -> str:
    """The admin boundary, pushed every run. Missing brief -> refuse to run.

    An admin agent with write tools and no boundary is exactly the thing this
    module exists to prevent, so its absence is loud, not silent (same rule as
    the executor brief)."""
    if not _BRIEF_PATH.exists():
        raise FileNotFoundError(
            f"admin boundary missing at {_BRIEF_PATH} — the admin subagent must "
            "not run without its written boundary"
        )
    return _BRIEF_PATH.read_text(encoding="utf-8").strip()


def build_admin_registry() -> tuple[list[dict[str, Any]], dict[str, Callable]]:
    """(schemas, registry) for the admin's explicit toolset — exactly ADMIN_TOOLS."""
    return ([schema_for(fn) for fn in ADMIN_TOOLS],
            {fn.__name__: fn for fn in ADMIN_TOOLS})


def admit(identity: Identity, confirmed: bool) -> Optional[AgentResult]:
    """The door. Returns a `blocked` envelope to refuse, or None to let the run in.

    Both conditions are required (decision #48). The refusal is `blocked`, not
    `failed`: a non-admin being turned away is the system working, not a defect.
    """
    if not getattr(identity, "is_admin", False):
        return AgentResult.blocked(
            "not an admin — this id is not on the admin allowlist", agent="admin")
    if not confirmed:
        return AgentResult.blocked(
            "admin action needs an explicit in-channel confirmation; identity "
            "alone does not authorise a privileged run", agent="admin")
    return None


def run_admin(
    request: str,
    *,
    identity: Identity,
    confirmed: bool,
    impact: tuple[str, ...] = (),
    approve: Optional[Callable[[str, dict], bool]] = None,
    model: str = STRONG,
    max_steps: int = MAX_STEPS,
    verbose: bool = True,
    run_log: Any = None,
) -> AgentResult:
    """Run the privileged subagent — but only past both locks.

    The gate is checked BEFORE anything touches the model or a store, so a
    refused request never spends a token and never opens a connection. Write
    authority is granted for this run only, through the ambient impact set.
    """
    refusal = admit(identity, confirmed)
    if refusal is not None:
        return refusal

    schemas, registry = build_admin_registry()
    session = identity.session

    with session_scope(session.user_id, session.thread_id):
        with impact_scope(impact):
            context = assemble(
                base_system=load_admin_brief(),
                query=request,
                impact=list(impact),
                session=session,
            )
            result = run_agent(
                request,
                schemas,
                registry,
                approve=approve,
                model=model,
                max_steps=max_steps,
                verbose=verbose,
                context=context,
                run_log=run_log,
            )

    stopped = result.get("stopped")
    if stopped == "answered":
        return AgentResult.ok(
            {"answer": result.get("answer"), "trace": result.get("trace", []),
             "granted_impact": list(impact)},
            agent="admin", notes=f"admin run for {session.user_id}")
    if stopped == "declined":
        return AgentResult.blocked(
            "a gated admin action was declined at the approval gate",
            agent="admin", result={"trace": result.get("trace", [])})
    return AgentResult.failed(
        f"admin run stopped: {stopped}", agent="admin",
        result={"trace": result.get("trace", [])})
