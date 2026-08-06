"""safety.patterns — the pattern catalogue, as data (HW6, T15.14).

Owner: Dias Sarkytbaev.

Patterns live here and nowhere else, for the same reason `guards.GATED` is one
set and `monitor/rubric.md` is one file: a rule that is copied into two scanners
drifts, and the copy that drifts is the one nobody re-reads. `input_filter` and
`detect` both read this module; neither owns a regex of its own.

**A pattern is a signal, not a verdict.** Every entry carries the family it
belongs to and a `why` line, and the family decides what the finding is *about*
once the caller says where the text came from:

    the same "ignore your previous instructions" is
      direct_injection    when it arrives in the user's own message
      indirect_injection  when it arrives inside a decision, a memory or a
                          retrieved passage the agent pulled in mid-run

That distinction is the whole point of splitting the two, and it cannot be made
by the regex — only the caller knows the channel. `input_filter.scan_input`
passes it explicitly rather than defaulting, so a new call site has to think
about it.

**Calibrated against the legitimate set, not against imagination.** Every pattern
here was checked against the 13 task texts in `eval/agent_cases.json`, which are
the traffic the false-positive rate is measured on (T15.18). Two near-misses are
worth recording because they shaped the regexes:

  * **ae07 asks the agent to delete a graph node.** Destructive *intent* is
    therefore legitimate traffic in this system, and naming a destructive tool
    is not evidence of anything — the approval gate exists precisely so that a
    user may ask. There is no "mentions prune_graph_node" pattern, and adding
    one would flag the gate working as an attack.
  * **ae10 says "Remember for next time: ..."** — an ordinary memory write. The
    memory-injection patterns require a *privilege* claim ("remember that I am
    an admin", "deletions are pre-approved"), never the word "remember".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

# Families, and what each one means. `input_filter` maps a family to a `Finding`
# code using the channel the text arrived on.
FAMILIES: tuple[str, ...] = (
    "override",           # "ignore your instructions"
    "role_hijack",        # "you are now in maintenance mode"
    "authority_forgery",  # "this is pre-approved by the developer"
    "secrecy",            # "do not mention this in your reply"
    "exfil_request",      # "print the private memory / your system prompt"
    "memory_injection",   # "remember that I am an admin"
)


@dataclass(frozen=True)
class Pattern:
    """One named check. `rule` is what a false-positive report can point at."""

    rule: str
    family: str
    severity: str
    regex: re.Pattern[str]
    why: str

    def find(self, text: str) -> Iterator[re.Match[str]]:
        return self.regex.finditer(text or "")


def _p(rule: str, family: str, severity: str, pattern: str, why: str) -> Pattern:
    return Pattern(rule, family, severity,
                   re.compile(pattern, re.IGNORECASE | re.DOTALL), why)


# --- injection-shaped text ----------------------------------------------------
#
# Severity here is the severity of ONE pattern firing alone. `input_filter`
# escalates when two families fire in the same text, because a payload that
# overrides *and* asks for silence is not a turn of phrase (see `combination`).

INJECTION_PATTERNS: tuple[Pattern, ...] = (
    _p("override_imperative", "override", "likely",
       r"\b(ignore|disregard|forget|override)\b(?:\W+\w+){0,4}?\W+"
       r"(instructions?|prompt|rules?|guardrails?|directives?|guidelines?)\b",
       "the classic override opener; an imperative aimed at the system prompt"),
    _p("override_above", "override", "likely",
       r"\b(ignore|disregard)\b(?:\W+\w+){0,3}?\W+(everything )?(above|earlier|so far)\b",
       "the same move without naming the instructions"),
    _p("role_hijack_mode", "role_hijack", "likely",
       r"\b(system override|protocol notice|admin(istrator)? mode|maintenance mode|"
       r"developer mode|debug mode|god mode|jailbreak)\b",
       "a mode this system does not have, announced as though it did"),
    _p("role_hijack_you_are_now", "role_hijack", "likely",
       r"\byou(?:'re| are) now\b(?:\W+\w+){0,6}?\W+"
       r"(admin(istrator)?|root|maintenance|developer|unrestricted|uncensored)\b",
       "re-declaring the agent's role mid-conversation"),
    _p("authority_forgery", "authority_forgery", "likely",
       r"\b(this (message|instruction|note) is )?from (the )?"
       r"(system|developer|operator|root|security team|your (developer|operator|vendor))\b",
       "quoted text claiming the authority of the chain of command it sits outside of"),
    _p("authority_preapproved", "authority_forgery", "likely",
       r"\b(pre-?approved|already approved|approval (is )?not required|"
       r"no (confirmation|approval) (is )?(needed|required)|you (already )?have permission)\b",
       "an attempt to talk past the approval gate in prose rather than at it"),
    _p("secrecy", "secrecy", "likely",
       r"\b(do not|don'?t|never)\b(?:\W+\w+){0,4}?\W+"
       r"(mention|tell|reveal|disclose|log|record|report|show)\b",
       "legitimate requests do not ask the agent to hide what it did"),
    _p("secrecy_without_telling", "secrecy", "likely",
       r"\bwithout (telling|informing|notifying|asking)\b",
       "the same instruction phrased as a condition"),
    _p("exfil_prompt", "exfil_request", "likely",
       r"\b(reveal|print|show|repeat|output|dump|display)\b(?:\W+\w+){0,6}?\W+"
       r"(system prompt|your (system )?instructions|operating rules verbatim|"
       r"api[_ -]?key|secret|token|password|credentials?|\.env)\b",
       "asks for the material the agent holds but does not own"),
    _p("exfil_other_user", "exfil_request", "likely",
       r"\b(other|another|someone else'?s?|every)\b(?:\W+\w+){0,3}?\W*users?'?s?\b"
       r"(?:\W+\w+){0,4}?\W+(data|memory|memories|decisions?|records?|private)\b",
       "visibility is a WHERE clause (#24), so this fails — but asking is still the attack"),
    _p("exfil_private_store", "exfil_request", "likely",
       r"\b(print|show|list|dump|read)\b(?:\W+\w+){0,4}?\W+"
       r"(the )?(full |all )?(private (memory|memories|decisions?)|store/|runs\.jsonl)\b",
       "names the agent's own stores, which CLAUDE.md §7.2 puts off limits to tools"),
    _p("memory_injection_admin", "memory_injection", "likely",
       r"\bremember\b(?:\W+\w+){0,4}?\W+(i (a|')?m|you are|the user is)\b"
       r"(?:\W+\w+){0,3}?\W+(an? )?(admin(istrator)?|owner|root|superuser|authori[sz]ed)\b",
       "the memory-injection attack: get the privilege claim STORED, then it is "
       "context on every future run (decision #26 keeps it out of instructions)"),
    _p("memory_injection_preapproval", "memory_injection", "likely",
       r"\b(deletions?|writes?|changes?|prunes?)\b(?:\W+\w+){0,3}?\W+"
       r"(are|is)\b(?:\W+\w+){0,2}?\W+(pre-?approved|allowed|authori[sz]ed|fine)\b",
       "the same attack aimed at the gate rather than at identity"),
)


# --- what must not leave ------------------------------------------------------
#
# Deliberately NARROW. This repo's own corpus is full of talk about
# `knowledge_graph.json`, `runs.jsonl` and `store/`, and an answer explaining the
# architecture is the most ordinary traffic there is — ae05 and ae06 are exactly
# that. Flagging a store path as exfiltration would make the false-positive rate
# a measure of how much the agent talks about itself. So: secret SHAPES, and the
# one file that holds real credentials.

SECRET_PATTERNS: tuple[Pattern, ...] = (
    _p("secret_openai_key", "exfil_request", "confirmed",
       r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{16,}", "an OpenAI/OpenRouter-shaped key"),
    _p("secret_github_token", "exfil_request", "confirmed",
       r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{20,}",
       "a GitHub token"),
    _p("secret_aws_key", "exfil_request", "confirmed",
       r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key id"),
    _p("secret_telegram_token", "exfil_request", "confirmed",
       r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b", "a Telegram bot token — the token IS the account"),
    _p("secret_private_key", "exfil_request", "confirmed",
       r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a PEM private key"),
    _p("secret_bearer", "exfil_request", "confirmed",
       r"\b[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}", "an Authorization header value"),
    _p("secret_assignment", "exfil_request", "likely",
       r"\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}",
       "a credential-shaped assignment, whatever the value turns out to be"),
    _p("secret_dotenv", "exfil_request", "likely",
       r"(?:^|[\s/\\`\"'])\.env\b",
       "the one file in this repo that holds real credentials; §7.2 puts it out of reach"),
)

# A URL is only interesting when it could CARRY something out: a query string or
# a long path segment. A bare "https://github.com/..." in an answer about the
# roadmap is not an exfiltration channel, and the README's own links are in the
# retrieval corpus, so treating every URL as a finding would flag ae05-shaped
# answers for quoting their own source.
URL_WITH_PAYLOAD = re.compile(
    r"https?://[^\s<>\"')]+(?:\?[^\s<>\"')]{8,}|/[A-Za-z0-9_\-%.+=]{24,})",
    re.IGNORECASE,
)

# The framing `agentlib/context.py` wraps quoted data in. Seeing it come back out
# in an ANSWER means the data channel and the answer channel got mixed — either
# the model echoed the wrapper, or something escaped it. Layer 2 is what keeps
# these apart, so this is the check that tells us Layer 2 held (decision #26).
FENCE_LEAK = re.compile(
    r"</?(retrieved-context|quoted-decision|quoted-memory)\b", re.IGNORECASE
)

# A tool call written as PROSE instead of issued as a call. Observed on the
# strong model when a prompt described tools it had not been given (#83); in an
# answer it means the model narrated an action rather than taking one, which is
# the shape of a run that claims to have done something it did not.
TOOL_CALL_SHAPED = re.compile(
    r"\b(CALLTYPE|tool_call|functions?\.)\s*[\(:]|"
    r"\b(prune_graph_node|apply_change|save_memory|append_decision_record)\s*\(",
    re.IGNORECASE,
)

# Internal record shapes. An answer may legitimately *discuss* decisions; it has
# no reason to contain the overlay's own column names as a JSON blob.
RECORD_DUMP = re.compile(
    r"[\"']?(decision_id|memory_id|run_id|author_id)[\"']?\s*:\s*[\"'][^\"']+[\"']|"
    r"[\"']?visibility[\"']?\s*:\s*[\"']private[\"']",
    re.IGNORECASE,
)


def combination(families: set[str]) -> bool:
    """Do these families, together, stop being a coincidence?

    One override phrase can be a quotation, a question about prompt injection, or
    a badly worded request — this repo's own documentation contains several. Two
    *different* families in one message is a payload: "ignore your instructions"
    plus "do not mention this" has no innocent reading, and neither does a role
    hijack that also asks for the private memory.

    Kept as a function rather than an `if len(...) > 1` at the call site because
    it is the rule most likely to need tuning once the false-positive count is in.
    """
    return len(families) >= 2
