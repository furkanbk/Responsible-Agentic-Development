"""safety.input_filter — Layer 1, detection over text coming in (HW6, T15.14).

Owner: Dias Sarkytbaev.

**Detection, never silent rewriting.** A filter that edits the user's message
hides the attack from every record downstream of it — the run log, the span
inputs, and the trace this whole phase is graded on. It also destroys the one
artefact an incident review needs: what was actually sent. So `scan_input`
returns findings and changes nothing, and no caller in this package mutates
text either.

That is not a soft position. The three layers that actually *stop* things in
this system are all structural and all predate HW6:

    Layer 2  `agentlib/context.py` fences retrieved material and neutralises
             wrapper-closing text; decision #26 keeps stored text out of
             `instructions` entirely.
    Layer 4  `guards.GATED` + the approve callback; `store/` refused by
             `read_source_file` and `apply_change`; an empty `impact_scope`
             denying every write; `detect_stall`; `validate_args`.
    the SQL  visibility is a `WHERE` clause (#24), so an obeyed injection asking
             for another user's rows still gets none.

A pattern list cannot join that group, because a pattern list can be phrased
around. What it can do is *say what happened*, which is what makes the four
attack classes countable — and what the report has to publish a false-positive
rate for.

One consequence worth stating: **`scan_input` is not in the request path.** It
is not called before `run_agent`, and nothing blocks on it. It runs over the
trace, after the fact, in `safety.detect` — the same out-of-band shape
`monitor/judge.py` uses (decision #40), and for the same reason: a detector on
the hot path is a new way for a run to fail.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .patterns import INJECTION_PATTERNS, Pattern, combination
from .types import Finding, clip, dedupe

# Which `Finding.code` a pattern family produces, given where the text came from.
# The channel decides, not the regex: identical text is a direct injection in the
# user's message and an indirect one inside a decision the agent pulled in.
CHANNELS: tuple[str, ...] = ("request", "data")

_CODE_BY_CHANNEL = {
    "request": "direct_injection",
    "data": "indirect_injection",
}


def scan_input(
    text: str,
    *,
    channel: str = "request",
    where: Optional[str] = None,
    patterns: Iterable[Pattern] = INJECTION_PATTERNS,
) -> list[Finding]:
    """Findings for one piece of incoming text. Never modifies it.

    Args:
      text     the message, passage, or tool output to scan.
      channel  `"request"` (the user typed it) or `"data"` (the agent pulled it
               in mid-run). Explicit rather than defaulted-by-guessing: it is
               the difference between the two injection classes, and only the
               caller knows.
      where    a label for the report — `"request"`, `"chunk:c17"`,
               `"tool.retrieve_decisions"`. Defaults to the channel.

    Escalation: one family firing is `likely` (a quotation, a question about
    prompt injection, or clumsy phrasing can all do it — this repo's own docs
    would trip `override_imperative` on several paragraphs). Two different
    families in one text is `confirmed`: "ignore your instructions" plus "do not
    mention this" has no innocent reading.
    """
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}, got {channel!r}")
    text = text or ""
    if not text.strip():
        return []

    code = _CODE_BY_CHANNEL[channel]
    label = where or channel
    findings: list[Finding] = []
    families: set[str] = set()

    for pattern in patterns:
        for match in pattern.find(text):
            families.add(pattern.family)
            findings.append(Finding(
                code=code,
                severity=pattern.severity,
                rule=pattern.rule,
                where=label,
                evidence=clip(match.group(0)),
                note=pattern.why,
                meta={"family": pattern.family, "channel": channel},
            ))

    findings = dedupe(findings)
    if combination(families):
        findings = [_escalate(f, sorted(families)) for f in findings]
    return findings


def _escalate(finding: Finding, families: list[str]) -> Finding:
    """Re-issue a finding at `confirmed`, saying which families co-occurred.

    Rebuilt rather than mutated because `Finding` is frozen — and it is frozen
    because a detector whose findings can be edited after the fact is a detector
    whose report cannot be trusted.
    """
    if finding.severity == "confirmed":
        return finding
    return Finding(
        code=finding.code,
        severity="confirmed",
        rule=finding.rule,
        where=finding.where,
        evidence=finding.evidence,
        note=finding.note + f" — escalated: {', '.join(families)} in one text",
        meta={**finding.meta, "escalated_by": families},
    )


def scan_data(text: str, *, where: str) -> list[Finding]:
    """`scan_input` for text the agent PULLED IN — the indirect-injection path.

    A separate name because the two call sites read very differently at a glance,
    and because getting the channel wrong silently mislabels every finding in a
    report whose whole structure is "which of the four attack classes was this".
    """
    return scan_input(text, channel="data", where=where)
