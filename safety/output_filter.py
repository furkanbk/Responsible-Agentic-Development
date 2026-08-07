"""safety.output_filter — Layer 3, checks on what leaves (HW6, T15.15).

Owner: Dias Sarkytbaev.

Three checks, which the brief names as one layer and which fail in three
different ways:

    schema         is this the SHAPE an answer may be? Not "is it true" — is it
                   prose to a human, or is it our own data channel coming back
                   out, a record dump, or a tool call narrated as text.
    citation       does a quoted sentence actually appear in what the run
                   retrieved? Reuses `eval/generation_metrics.py::quote_is_present`
                   (T15.15 says import it, and the alternative is a second,
                   subtly different quote matcher in the same repo).
    exfiltration   is something in here that should not have left — a
                   credential-shaped string, `.env`, a URL that could carry a
                   payload out.

**The citation check is undefined, not clean, when there is nothing to check
against.** A run that retrieved nothing has no passages a quote could be found
in, so the answer is unverifiable rather than unfaithful — and scoring it as a
violation would flag every arithmetic answer in the eval set. That is #67's
"empty golden is `None`, not 0" and `monitor/judge.py`'s ungradeable verdict,
applied a third time. Callers that report coverage say "not checked", never
"clean".

**Deliberately not here: a check that the answer 'refused correctly'.** Whether
a refusal was the right call is a judgement, and this module is the part of the
safety layer that makes no judgements — `monitor/judge.py` already grades
behaviour with a model, out of band, and duplicating that here with regexes
would produce a second, worse opinion under a name that sounds mechanical.
"""

from __future__ import annotations

import re
from typing import Optional

from .patterns import (
    FENCE_LEAK,
    RECORD_DUMP,
    SECRET_PATTERNS,
    TOOL_CALL_SHAPED,
    URL_WITH_PAYLOAD,
)
from .types import Finding, clip, dedupe

# Quoted spans the citation check looks at: straight and typographic double
# quotes only.
#
# **Backticks are deliberately NOT citations, and this was measured.** The first
# live pass flagged ae13 — a perfectly ordinary answer — because the model wrote
# `` `query_component_graph("task-list app")` `` while explaining what it had
# done. In a codebase agent, a backticked span is an identifier or a call, not a
# quotation from a passage, and treating one as a citation makes the answer's own
# vocabulary look like a fabricated source. That single rule was the entire
# false-positive count on legitimate traffic.
#
# Single quotes are excluded for a duller reason: apostrophes make them
# unparseable, and "it's" is not a citation.
_QUOTED = re.compile(r"\"([^\"]{12,400})\"|“([^”]{12,400})”", re.DOTALL)


def scan_output(
    answer: Optional[str],
    *,
    sources: str = "",
    request: str = "",
    stopped: Optional[str] = None,
) -> list[Finding]:
    """Every Layer-3 finding for one answer.

    Args:
      answer   the text the run produced. `None` is not a violation on its own —
               a declined or stalled run has no answer by design.
      sources  everything the run actually saw: retrieved passage text and tool
               outputs, concatenated. The haystack for the citation check and
               the "was this URL already in front of it" test.
      request  the user's own message. A quote of the user's words is cited from
               the conversation, not invented, so it counts as grounded.
      stopped  the loop's stopping condition, for the one schema rule that needs
               it (`answered` with no answer).
    """
    findings: list[Finding] = []
    findings += schema_findings(answer, stopped=stopped)
    findings += exfiltration_findings(answer, sources=sources, request=request)
    findings += citation_findings(answer, sources=sources, request=request)
    return dedupe(findings)


# --- schema -------------------------------------------------------------------

def schema_findings(answer: Optional[str], *, stopped: Optional[str] = None) -> list[Finding]:
    """Constraints on the shape of what leaves. Cheap, mechanical, no model."""
    findings: list[Finding] = []

    if stopped == "answered" and not (answer or "").strip():
        findings.append(Finding(
            code="schema_violation", severity="suspicious", rule="answered_with_no_answer",
            where="answer",
            note="the loop reported `answered` but there is no text — a stopping "
                 "condition and an empty result should not co-occur",
        ))
    if not answer:
        return findings

    for match in FENCE_LEAK.finditer(answer):
        findings.append(Finding(
            code="schema_violation", severity="likely", rule="data_fence_leaked",
            where="answer", evidence=clip(match.group(0)),
            note="the quoted-data wrapper from agentlib/context.py came back out in "
                 "the answer — either the model echoed the framing or something "
                 "escaped it, and Layer 2 is exactly what keeps the two channels apart",
        ))
    for match in TOOL_CALL_SHAPED.finditer(answer):
        findings.append(Finding(
            code="schema_violation", severity="suspicious", rule="tool_call_as_prose",
            where="answer", evidence=clip(match.group(0)),
            note="a tool call written as text instead of issued as one; the run may "
                 "be narrating an action it never took (the failure #83 records)",
        ))
    for match in RECORD_DUMP.finditer(answer):
        findings.append(Finding(
            code="schema_violation", severity="likely", rule="internal_record_dump",
            where="answer", evidence=clip(match.group(0)),
            note="the overlay's own column names in the answer body; discussing a "
                 "decision is ordinary, emitting the row is not",
        ))
    return findings


# --- exfiltration -------------------------------------------------------------

def exfiltration_findings(
    answer: Optional[str], *, sources: str = "", request: str = ""
) -> list[Finding]:
    """Credential shapes, `.env`, and URLs that could carry something out."""
    if not answer:
        return []
    findings: list[Finding] = []

    for pattern in SECRET_PATTERNS:
        for match in pattern.find(answer):
            findings.append(Finding(
                code="exfiltration", severity=pattern.severity, rule=pattern.rule,
                where="answer", evidence=clip(match.group(0)), note=pattern.why,
            ))

    # A URL is only an exfiltration channel if it can carry a payload AND the run
    # did not get it from the material in front of it. The README's own links are
    # in the retrieval corpus, so an answer quoting one is citing its source.
    haystack = f"{sources}\n{request}"
    for match in URL_WITH_PAYLOAD.finditer(answer):
        url = match.group(0)
        if url in haystack:
            continue
        findings.append(Finding(
            code="exfiltration", severity="likely", rule="outbound_url_with_payload",
            where="answer", evidence=clip(url),
            note="a URL carrying a query string or a long path, present in neither "
                 "the request nor anything the run retrieved — the shape of "
                 "'encode it and put it in a link'",
        ))
    return findings


# --- citation -----------------------------------------------------------------

def citation_findings(
    answer: Optional[str], *, sources: str = "", request: str = ""
) -> list[Finding]:
    """Quoted spans in the answer that are in neither the passages nor the request.

    Returns `[]` when there is nothing to check against — see the module
    docstring. Undefined is not clean, and every caller reports it as coverage
    rather than as a pass.
    """
    if not answer or not (sources or "").strip():
        return []

    quote_is_present, min_chars = _quote_checker()
    haystack = f"{sources}\n{request}"
    findings: list[Finding] = []

    for match in _QUOTED.finditer(answer):
        quote = next((g for g in match.groups() if g), "")
        if len(" ".join(quote.split())) < min_chars:
            continue
        if quote_is_present(quote, haystack):
            continue
        findings.append(Finding(
            code="citation_unverified", severity="suspicious", rule="quote_not_in_sources",
            where="answer", evidence=clip(quote),
            note="quoted as though from the retrieved material, and not found in it "
                 "(same matcher the HW5 faithfulness scorer uses, fuzzy arm included)",
        ))
    return dedupe(findings)


def _quote_checker():
    """`quote_is_present` + its minimum length, imported lazily.

    Lazy on purpose. `eval.generation_metrics` pulls in `agentlib.core` (the
    OpenAI client) and `retrieval.types`, and `safety.detect` advertises itself
    as a pure function of a trace that a batch pass can run without standing up
    the agent or Postgres. Importing at module scope would make reading a trace
    require the whole answering stack — the exact coupling `tracing/` avoids by
    importing nothing from this repo at all.

    Reused rather than reimplemented (T15.15): its fuzzy arm exists because
    judges elide with "..." and fix punctuation, and a second quote matcher in
    the same repo would drift from that one silently.
    """
    from eval.generation_metrics import MIN_QUOTE_CHARS, quote_is_present

    return quote_is_present, MIN_QUOTE_CHARS
