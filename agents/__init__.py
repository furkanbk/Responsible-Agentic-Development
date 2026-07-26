"""agents — the multi-agent layer (HW2, Phase 6).

A planner and an executor, coordinated by plain Python. They pass work through
`AgentResult`, a small structured object, and the orchestrator branches on its
fields — never on either agent's prose.

Ownership (TODO.md): `envelope.py`, `executor.py`, `executor_brief.md` are
Berat's; `planner.py` is Alejandro's.
"""

from __future__ import annotations

from .envelope import AgentResult, Plan, empty_plan

__all__ = ["AgentResult", "Plan", "empty_plan"]
