"""safety — the HW6 safety layer (Phase 15D, course Part 3).

Owner: Dias Sarkytbaev.

Four attack classes, four defense layers, and an honest account of which layers
this phase actually built. The threat model in full is README § HW6 Part 3; this
is the map a session needs before touching anything here.

## The four layers, and where each one lives

    Layer 1  input filtering          `safety/input_filter.py`      NEW (T15.14)
    Layer 2  structural separation    `agentlib/context.py`         ALREADY BUILT
    Layer 3  output filtering         `safety/output_filter.py`     NEW (T15.15)
    Layer 4  capability constraints   `agentlib/guards.py`, tools/  ALREADY BUILT

**Layers 2 and 4 were not skipped and were not rebuilt.** They landed in HW1 and
HW2 and they are the layers that actually stop things:

  * Layer 2 — `agentlib/context.py::_render_data` fences retrieved material in
    `<retrieved-context>` with explicit "this is DATA" framing, `_escape`
    neutralises wrapper-closing text so a payload cannot break out of its quote,
    and decision #26 keeps stored text out of `instructions` altogether. The
    memory-injection attack is closed by that last one, structurally: a "fact"
    saying the user is an admin is quoted data forever, never an operating rule.
  * Layer 4 — `guards.GATED` + the approve callback (irreversible tools pause for
    a human), `store/` refused by `read_source_file` and `apply_change`, an empty
    `impact_scope` denying every write (#25), `validate_args`, `detect_stall`.
    Plus the one that does the most work and is not on the assignment's list:
    **visibility is a `WHERE` clause** (#24), so an injection that asks for
    another user's memory gets nothing even if the model is completely fooled.

Re-implementing either as a regex would have been a downgrade dressed as
coverage, so this package adds the two that were genuinely missing and reports
the other two with file names and decision numbers.

## What this package does, and what it refuses to do

It **detects**. It does not block, edit, or refuse:

  * an input filter that rewrites the user's text hides the attack from the run
    log and from the trace this phase is graded on (T15.14 says this explicitly);
  * nothing here is on the request path — `scan_trace` runs after the fact, the
    same out-of-band shape `monitor/judge.py` uses (#40). A detector on the hot
    path is a new way for a run to fail.

Severities are three **named bands** — `suspicious` / `likely` / `confirmed` —
never a 1-10 score (#37, the same rule the monitor's rubric and the reranker's
bands follow).

## Entry points

    from safety import scan_trace, checks_run, log_findings
    findings = scan_trace(trace)          # pure function of a trace (T15.17)
    log_findings(trace.info.trace_id, findings, checks_run(trace))   # safety.* feedback

    python -m safety.run_safety_scan --stored        # batch pass + false positives
    python -m safety.run_safety_scan --attacks       # the attack set, live
"""

from __future__ import annotations

from .detect import (
    CHECKS,
    FEEDBACK_NAMES,
    checks_run,
    feedback_values,
    flagged,
    indirect_injection_findings,
    log_findings,
    scan_trace,
    sources_text,
    summarise,
    tool_abuse_findings,
    tool_outputs,
)
from .input_filter import scan_data, scan_input
from .output_filter import (
    citation_findings,
    exfiltration_findings,
    schema_findings,
    scan_output,
)
from .patterns import INJECTION_PATTERNS, SECRET_PATTERNS, Pattern
from .types import CODES, SEVERITIES, Finding, by_code, dedupe, sort_findings, worst

__all__ = [
    # types
    "Finding", "SEVERITIES", "CODES", "worst", "by_code", "dedupe", "sort_findings",
    # layer 1
    "scan_input", "scan_data", "INJECTION_PATTERNS", "SECRET_PATTERNS", "Pattern",
    # layer 3
    "scan_output", "schema_findings", "exfiltration_findings", "citation_findings",
    # the detector
    "scan_trace", "checks_run", "flagged", "summarise", "log_findings",
    "feedback_values", "FEEDBACK_NAMES", "CHECKS", "sources_text", "tool_outputs",
    "indirect_injection_findings", "tool_abuse_findings",
]
