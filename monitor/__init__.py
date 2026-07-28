"""monitor — the LLM-as-judge that grades runs from outside the loop.

Owner: Dias Sarkytbaev (HW2, T7.3 / T7.4 / T7.5).

The monitor is a **separate job with a separate agent**. It runs on its own
clock, after the fact, over `store/runs/runs.jsonl`. It has no tools, no live
store, and no way to affect the run it is grading — the strongest isolation the
project has. This is the safety net one level up from the HW1 guards: those
caught a bad value *inside* the loop; the monitor grades the *whole run* from
the outside.
"""

from __future__ import annotations

from .judge import (
    ADHERENCE,
    GROUNDING,
    Verdict,
    judge_run,
    judge_runs,
    load_rubric,
    problems,
    report,
)

__all__ = [
    "ADHERENCE",
    "GROUNDING",
    "Verdict",
    "judge_run",
    "judge_runs",
    "load_rubric",
    "problems",
    "report",
]
