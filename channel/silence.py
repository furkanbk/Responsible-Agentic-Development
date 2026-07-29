"""channel.silence — the decision to say nothing, and the record of it.

Owner of this file's CONTRACT: Berat Furkan Kocak (HW3, T9.0b).
Owner of `evaluate_silence`'s BODY: Dias Sarkytbaev (HW3, T11.2).

**This is a stub in the T0.8 sense — it is a contract, not a gap.** The
dataclass and the signature below are frozen; the body is Dias's task and filling
it in here takes his work away from him (CLAUDE.md §1). If the signature is wrong
for what T11.2 needs, say so and we change it together.

Why silence needs a type at all
-------------------------------
"The agent didn't answer" and "the agent decided not to answer" are the same
observation and completely different events. Without a value that says which one
happened, a deliberate non-answer is indistinguishable from a crash, and the
first time somebody debugs one they will "fix" the other.

So silence is expressible (`OutboundReply.quiet`), recorded (`silences`, T9.4),
and attributable (`reason_code`). A silence with a reason on file is a correct
outcome. A silence without one is an outage.

The primary case, and why it is sharper than it looks
-----------------------------------------------------
Someone asks in a shared thread about a component whose only relevant decisions
are private to a different user. The obviously wrong answer is to quote them.

The subtly wrong answer is "there's a private decision about that, ask Dias." It
leaks less, so it feels safe, and it is still a leak: it discloses that a record
exists and who owns it. Existence is content. Under `visible_to` the asker's
query returned nothing, and the honest rendering of nothing is nothing.

Which is also why this must be decided from the QUERY RESULT and not by handing
the model both sets of rows with an instruction to keep one of them quiet
(decision #24). The guard is a comparison of two counts. It is not a judgement
call, and it must not become one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from agentlib.session import SessionKey

from .base import InboundEvent

#: Silence is deliberate, so its reasons are a closed set. A free-text reason
#: would drift into prose nobody queries, and the point of T9.4 is that these
#: are counted and read back.
REASON_HEARTBEAT_CLEAN = "heartbeat_clean"          # T11.1 — nothing to report
REASON_PRIVATE_DECISION = "private_decision_leak"   # T11.2 — the primary branch
REASON_NO_DECISIONS_TOUCHED = "no_decisions_touched"  # T10.3 — irrelevant push
REASON_INJECTION_ATTEMPT = "injection_attempt"      # quoted text tried to steer

REASON_CODES: frozenset[str] = frozenset({
    REASON_HEARTBEAT_CLEAN,
    REASON_PRIVATE_DECISION,
    REASON_NO_DECISIONS_TOUCHED,
    REASON_INJECTION_ATTEMPT,
})

TEAM = "team"


@dataclass(frozen=True)
class SilenceDecision:
    """Whether to stay quiet, why, and who is allowed to know why.

    Attributes:
      silent      False means answer normally; the other fields are then unused.
      reason_code one of REASON_CODES. A closed set so silences are countable.
      evidence    what was CHECKED — uids, counts, the asker. Never the content
                  that triggered the silence. A leak guard that writes the
                  private decision into its own audit row has moved the leak,
                  not closed it.
      visibility  'team' or 'user:<id>' — who may later read this reason. For
                  the private-decision case this is the OWNER of the withheld
                  decision, not the person who asked: the owner should be able
                  to see that somebody went looking, and nobody else should.
    """

    silent: bool
    reason_code: str = ""
    evidence: str = ""
    visibility: str = TEAM

    @classmethod
    def speak(cls) -> "SilenceDecision":
        """The ordinary outcome: nothing is being withheld, answer the request."""
        return cls(silent=False)


def evaluate_silence(
    event: InboundEvent,
    session: SessionKey,
    candidates: list[dict[str, Any]],
) -> SilenceDecision:
    """Decide whether this event must go unanswered.

    **Body owned by Dias (T11.2). Do not implement this if it is not your task.**

    Args:
      event       the inbound event under consideration.
      session     the identity resolved for it (`channel.identity.resolve`).
                  `session.user_id` is who the visibility filter ran as.
      candidates  the decision rows that WOULD have been relevant to the asked
                  about component, fetched WITHOUT the visibility filter — the
                  unfiltered side of the comparison. Each row carries at least
                  `decision_id`, `symbol_uid`, `visibility` and `author_id`.

                  Passing the unfiltered rows here is deliberate and is the one
                  place in the system that sees across scopes. It is safe only
                  because this function returns a *decision*, never text: the
                  rows die on this stack frame and nothing they contain reaches
                  the model or the channel. Do not return their content in
                  `evidence`, and do not pass them onward.

    Returns:
      A SilenceDecision. `SilenceDecision.speak()` when the request should be
      answered normally.

    The expected shape of the primary case (T11.2): the visibility-filtered
    query returned nothing while `candidates` is non-empty and every row in it
    is private to somebody other than `session.user_id` — then stay silent with
    `REASON_PRIVATE_DECISION`, `visibility` set to the owner's scope, and
    `evidence` naming only uids and counts.
    """
    # No relevant decisions at all -> nothing is being withheld. Answer.
    if not candidates:
        return SilenceDecision.speak()

    asker = session.user_id  # "" for the anonymous/system identity

    # Replicate the visibility rule (overlay.db.visible_to) here rather than
    # trusting the model with the rows: a row is visible to the asker iff it is
    # team-wide, or private to the asker themselves (decision #24). This is the
    # filtered side of the comparison, computed from the same rows.
    def _visible_to_asker(row: dict[str, Any]) -> bool:
        vis = str(row.get("visibility") or "")
        if vis == TEAM:
            return True
        return bool(asker) and vis == f"user:{asker}"

    # If the asker can see even one relevant decision, there is nothing to hide
    # from them — including the owner asking about their own private decision.
    if any(_visible_to_asker(row) for row in candidates):
        return SilenceDecision.speak()

    # The asker sees NONE, yet rows exist. Silence only when every one is private
    # to someone else; if any row is unclassifiable, do not guess — answer.
    owners = {str(row.get("visibility") or "") for row in candidates}
    if not all(o.startswith("user:") for o in owners):
        return SilenceDecision.speak()

    # This is the leak-guard case: the visibility-filtered view is empty while the
    # unfiltered view is not, and everything in it belongs to another user.
    uids = sorted({str(row.get("symbol_uid") or "") for row in candidates})
    # Record who owns the withheld knowledge so THAT user (and nobody else) can
    # later see that someone went looking. If several owners, the record is
    # scoped to one of them; each still learns only that a lookup happened.
    owner_scope = sorted(owners)[0]
    evidence = (
        f"asker={asker or 'anonymous'} could see 0 of {len(candidates)} "
        f"decision(s) on {','.join(uids) or '(no uid)'}; all private to another "
        f"user. Withheld to avoid disclosing existence/ownership."
    )
    # `evidence` names uids, counts and the asker only — never the decision text.
    return SilenceDecision(
        silent=True,
        reason_code=REASON_PRIVATE_DECISION,
        evidence=evidence,
        visibility=owner_scope,
    )


def is_valid_reason(reason_code: str) -> bool:
    """True iff `reason_code` is one of the recorded silence reasons."""
    return reason_code in REASON_CODES


def owner_scope(record: dict[str, Any]) -> Optional[str]:
    """The visibility scope of whoever owns `record`, or None if it is team-wide.

    Helper for T11.2: the silence record must be readable by the decision's
    owner, and this is where "owner" is read off a row so the answer is the same
    everywhere it is asked.
    """
    visibility = str(record.get("visibility") or "")
    return visibility if visibility.startswith("user:") else None
