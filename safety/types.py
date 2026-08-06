"""safety.types — what a safety check returns (HW6, Phase 15D).

Owner: Dias Sarkytbaev.

One shape for every layer, so a finding from the input filter, the output filter
and the trace detector can sit in the same list, be counted the same way, and be
written back under the same `safety.*` namespace (contract #14).

**Named severities, never a 1-10 score** — decision #37, the same rule
`monitor/judge.py` follows for adherence and `retrieval/rerank.py` follows for
relevance bands. A model asked for a number clusters everything at 7; three named
bands force a commitment a human can audit:

    confirmed    mechanically true, no judgement involved. A gated call
                 re-issued after the code declined it. A key-shaped string in an
                 answer. There is nothing to argue about.
    likely       a pattern fired where that pattern has no legitimate reason to
                 be — an instruction-shaped imperative inside retrieved DATA.
    suspicious   worth a human's attention, and expected to produce some false
                 positives. Reported, never acted on automatically.

Nothing in this package acts on a finding: it is a detector, not an enforcer.
The layers that *stop* things (the approval gate, the visibility predicate, the
denylists) are code that already exists and predates this phase — see the README
§ Part 3 threat model for which layer does what.

`rule` is the id of the check that fired. It is what makes a false positive
fixable: "the detector is noisy" is unactionable, "`override_imperative` fires on
ae06" names one regex.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

# Ordered weakest -> strongest. The index is used for sorting and for "the worst
# severity seen", and for nothing else — it is deliberately not exported as a
# number a report could average (#37).
SEVERITIES: tuple[str, ...] = ("suspicious", "likely", "confirmed")

# What a finding is ABOUT. The four attack classes the assignment names, plus the
# two output-side checks that are properties of an answer rather than attacks.
CODES: tuple[str, ...] = (
    "direct_injection",       # the user's own message carries the payload
    "indirect_injection",     # the payload arrives through data the agent pulled
    "tool_abuse",             # tools used in a way the guards already rejected
    "exfiltration",           # something that should not leave, leaving
    "citation_unverified",    # the answer quotes passages that do not contain it
    "schema_violation",       # the answer is not the shape an answer may be
)

MAX_EVIDENCE_CHARS = 200


def clip(text: Any, limit: int = MAX_EVIDENCE_CHARS) -> str:
    """Collapse whitespace and cap length — evidence is for a human to read.

    Evidence is quoted attacker-controlled text. It is stored and printed, never
    re-fed to a model, so the only requirement is that a person can recognise
    what fired without scrolling past a 40 KB chunk.
    """
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + " […]"


@dataclass(frozen=True)
class Finding:
    """One thing a check noticed, with enough context to check it by hand."""

    code: str
    severity: str
    rule: str
    where: str                      # "request" | "answer" | "tool.<name>" | "chunk:<id>"
    evidence: str = ""
    note: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A typo'd code or severity silently drops the finding out of every
        # count that groups by them, which is the same class of bug
        # `request_origin_scope` refuses loudly for tags.
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {self.severity!r}")
        if self.code not in CODES:
            raise ValueError(f"code must be one of {CODES}, got {self.code!r}")

    def __str__(self) -> str:
        head = f"[{self.severity}] {self.code}/{self.rule} in {self.where}"
        return f"{head}: {self.evidence}" if self.evidence else head

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def worst(findings: Iterable[Finding]) -> Optional[str]:
    """The strongest severity present, or None for a clean scan.

    `None` means "nothing fired", which is not the same as "checked and safe" —
    every caller that reports this distinguishes the two, because a detector
    that cannot run reads exactly like a detector that found nothing.
    """
    seen = [f.severity for f in findings]
    if not seen:
        return None
    return max(seen, key=SEVERITIES.index)


def by_code(findings: Iterable[Finding]) -> dict[str, list[Finding]]:
    """Group findings by code, preserving order within each group."""
    out: dict[str, list[Finding]] = {}
    for finding in findings:
        out.setdefault(finding.code, []).append(finding)
    return out


def dedupe(findings: Iterable[Finding]) -> list[Finding]:
    """Drop repeats of the same (code, rule, where, evidence).

    A payload that repeats a phrase six times is one finding, not six. Without
    this a single crafted message can dominate a false-positive count and make
    the rate meaningless — the number the report has to publish is "how many
    legitimate runs were flagged", and a flag is per run, not per match.
    """
    seen: set[tuple[str, str, str, str]] = set()
    out: list[Finding] = []
    for finding in findings:
        key = (finding.code, finding.rule, finding.where, finding.evidence)
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return out


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Strongest first, then by code, then by rule. Report order only."""
    return sorted(
        findings,
        key=lambda f: (-SEVERITIES.index(f.severity), f.code, f.rule, f.where),
    )
