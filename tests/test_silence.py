"""tests/test_silence.py — the private-decision leak guard (HW3, T11.2/T11.4).

Owner: Dias Sarkytbaev. New file.

`evaluate_silence` is a pure function of (event, session, candidates), so it needs
no model and no store — the whole guard is a comparison of two visibility views
over the rows it is handed. These tests pin the four cases the TODO names, plus
the metadata-leak property: the recorded `evidence` never carries decision text.
"""

from __future__ import annotations

from agentlib.session import SessionKey
from channel.base import InboundEvent
from channel.silence import (
    REASON_PRIVATE_DECISION,
    SilenceDecision,
    evaluate_silence,
)


def event(user_id: str | None = "1", text: str = "why is tools.decisions like that?"):
    return InboundEvent(source="telegram", thread_key="shared-thread",
                        text=text, external_user_id=user_id)


def decision(visibility: str, *, author: str = "bob", uid: str = "Module:tools.decisions",
             did: str = "d_ab12", text: str = "SECRET: we chose SQLite over JSON"):
    """A candidate row as evaluate_silence receives it — carries `decision` text
    on purpose, so a test can prove that text never reaches `evidence`."""
    return {"decision_id": did, "symbol_uid": uid, "visibility": visibility,
            "author_id": author, "decision": text}


ALICE = SessionKey(user_id="alice")
ANON = SessionKey(user_id="")


class TestSpeaks:
    def test_no_candidates_speaks(self):
        assert evaluate_silence(event(), ALICE, []) == SilenceDecision.speak()

    def test_a_team_decision_speaks(self):
        out = evaluate_silence(event(), ALICE, [decision("team")])
        assert out.silent is False

    def test_owner_asking_about_their_own_private_decision_speaks(self):
        out = evaluate_silence(event(), ALICE,
                               [decision("user:alice", author="alice")])
        assert out.silent is False

    def test_team_row_alongside_a_foreign_private_row_speaks(self):
        # The asker can see the team row, so there is nothing to withhold.
        out = evaluate_silence(event(), ALICE,
                               [decision("team"), decision("user:bob")])
        assert out.silent is False

    def test_an_unclassifiable_row_is_not_guessed_into_silence(self):
        out = evaluate_silence(event(), ALICE, [decision("weird-scope")])
        assert out.silent is False


class TestSilences:
    def test_a_foreign_private_decision_is_withheld(self):
        out = evaluate_silence(event(), ALICE, [decision("user:bob")])
        assert out.silent is True
        assert out.reason_code == REASON_PRIVATE_DECISION
        # The record is scoped to the OWNER, so only bob learns someone asked.
        assert out.visibility == "user:bob"

    def test_evidence_carries_uids_and_counts_but_never_the_decision_text(self):
        out = evaluate_silence(event(), ALICE, [decision("user:bob")])
        assert "SECRET" not in out.evidence
        assert "SQLite" not in out.evidence
        assert "Module:tools.decisions" in out.evidence   # uid is fine
        assert "alice" in out.evidence                     # the asker is fine

    def test_it_does_not_name_the_owner_as_prose_to_the_asker(self):
        # The withheld fact — existence and ownership — must not leak into any
        # text the asker could read. evidence is owner-scoped, not asker-facing.
        out = evaluate_silence(event(), ALICE, [decision("user:bob")])
        assert out.silent and out.visibility.startswith("user:")

    def test_anonymous_asker_is_also_silenced_on_a_foreign_private_row(self):
        out = evaluate_silence(event(user_id=None), ANON, [decision("user:bob")])
        assert out.silent is True
        assert out.reason_code == REASON_PRIVATE_DECISION

    def test_all_private_to_others_is_required(self):
        # One of the two is the asker's own -> asker sees something -> speak.
        out = evaluate_silence(event(), ALICE,
                               [decision("user:bob"), decision("user:alice", author="alice")])
        assert out.silent is False
