"""tests.test_channel — the HW3 channel foundation (Phase 9).

Owner: Berat Furkan Kocak (HW3, T9.8).

No API calls and no network. The Telegram tests drive `to_event` directly with
raw update dicts, which is the whole untrusted-input surface; the gate tests use
real threads but sub-second timeouts.

What these are actually protecting, in order of how much it would cost to get
wrong:

  1. an unmapped sender cannot become a mapped one (identity)
  2. a bystander can neither approve nor cancel someone else's write (gate)
  3. a silence recorded against one scope is invisible to every other (silences)
  4. a human's message is never dropped or merged (queue)
"""

from __future__ import annotations

import threading
import time

import pytest

from agentlib.approval import AFFIRMATIVE, ChannelGate, preview_args
from channel.base import InboundEvent, OutboundReply
from channel.identity import ANONYMOUS_USER_ID, BOT_AUTHOR_ID, resolve
from channel.queue import GATE_BUSY_REASON, WorkQueue, Worker
from channel.silence import (
    REASON_CODES,
    REASON_PRIVATE_DECISION,
    SilenceDecision,
    evaluate_silence,
    is_valid_reason,
    owner_scope,
)
from channel.telegram import TelegramChannel
from overlay import db as overlay_db


# --- helpers ------------------------------------------------------------------

def tg(text: str = "hello", uid: str | None = "42", thread: str = "grp") -> InboundEvent:
    return InboundEvent(source="telegram", thread_key=thread, text=text,
                        external_user_id=uid)


def gh(branch: str = "main") -> InboundEvent:
    return InboundEvent(source="github", thread_key="team",
                        dedupe_key=f"push:{branch}")


def hb() -> InboundEvent:
    return InboundEvent(source="heartbeat", thread_key="team")


@pytest.fixture
def allowlist(monkeypatch):
    monkeypatch.setenv("RADF_CHANNEL_USERS", "telegram:42=berat,telegram:7=dias")
    monkeypatch.setenv("RADF_CHANNEL_ADMINS", "berat")


# --- the event shape ----------------------------------------------------------

class TestInboundEvent:
    def test_human_messages_are_never_coalescable(self):
        """Merging a question somebody typed is data loss, not a queue policy."""
        assert tg().coalescable is False
        assert tg().interactive is True

    def test_machine_events_are_coalescable_and_not_interactive(self):
        assert gh().coalescable is True
        assert gh().interactive is False
        assert hb().interactive is False

    def test_silence_is_a_value_not_an_empty_string(self):
        quiet = OutboundReply.quiet("grp")
        assert quiet.silent is True
        assert quiet.text == ""
        assert OutboundReply(thread_key="grp", text="").silent is False


# --- identity -----------------------------------------------------------------

class TestIdentity:
    def test_mapped_sender_resolves_to_their_radf_id(self, allowlist):
        identity = resolve(tg(uid="42"))
        assert identity.session.user_id == "berat"
        assert identity.known and identity.can_write

    def test_unmapped_sender_does_not_become_their_own_scope(self, allowlist):
        """The platform id is a lookup key, never a RADF user id.

        If it were reused directly, the first stranger to message the bot would
        pick their own visibility scope.
        """
        identity = resolve(tg(uid="999999"))
        assert identity.session.user_id == ANONYMOUS_USER_ID
        assert identity.known is False
        assert identity.can_write is False

    def test_anonymous_sees_team_rows_only(self, allowlist):
        """The second, independent mechanism: the visibility SQL itself."""
        identity = resolve(tg(uid="999999"))
        sql, params = overlay_db.visible_to(identity.session.user_id)
        assert params == ["team"]
        assert "IN" not in sql

    def test_machine_events_inherit_nobody(self, allowlist):
        """A 3am trigger must not act with the last human's permissions."""
        for event in (gh(), hb()):
            identity = resolve(event)
            assert identity.session.user_id == ANONYMOUS_USER_ID
            assert identity.can_write is False

    def test_admin_flag_comes_from_the_allowlist(self, allowlist):
        assert resolve(tg(uid="42")).is_admin is True
        assert resolve(tg(uid="7")).is_admin is False

    def test_admin_of_an_unmapped_id_is_never_true(self, monkeypatch):
        """An admin allowlist naming a user nobody maps to grants nothing."""
        monkeypatch.setenv("RADF_CHANNEL_USERS", "")
        monkeypatch.setenv("RADF_CHANNEL_ADMINS", "berat")
        assert resolve(tg(uid="42")).is_admin is False

    def test_malformed_allowlist_entries_fail_closed(self, monkeypatch):
        monkeypatch.setenv("RADF_CHANNEL_USERS", "garbage,telegram:42=berat,=x,y=")
        assert resolve(tg(uid="42")).session.user_id == "berat"
        assert resolve(tg(uid="1")).known is False

    def test_thread_key_is_sanitised_and_bounded(self, allowlist):
        identity = resolve(tg(uid="42", thread="../../etc/passwd" + "x" * 200))
        assert len(identity.session.thread_id) <= 64
        assert "/" not in identity.session.thread_id

    def test_bot_writes_as_itself(self):
        """The disposable identity's second layer lives in the store."""
        assert BOT_AUTHOR_ID == "bot:radf"
        assert not BOT_AUTHOR_ID.startswith("user:")


# --- queue admission ----------------------------------------------------------

class TestQueuePolicy:
    def test_human_messages_queue_in_order(self):
        q = WorkQueue()
        for i in range(3):
            assert q.submit(tg(f"q{i}"), user_id="berat").disposition == "queued"
        assert [q.take(0).text for _ in range(3)] == ["q0", "q1", "q2"]

    def test_duplicate_push_coalesces_keeping_position(self):
        """Newest wins, position preserved — one busy branch cannot starve another."""
        q = WorkQueue()
        q.submit(gh("main"))
        q.submit(gh("dev"))
        first_dev = len(q)
        admission = q.submit(gh("main"))
        assert admission.disposition == "coalesced"
        assert len(q) == first_dev          # nothing added
        assert q.pending_sources() == ["github", "github"]
        assert q.take(0).dedupe_key == "push:main"   # still first

    def test_second_heartbeat_is_dropped_not_coalesced(self):
        """The queued one already covers everything outstanding."""
        q = WorkQueue()
        assert q.submit(hb()).disposition == "queued"
        assert q.submit(hb()).disposition == "dropped"
        assert len(q) == 1

    def test_full_queue_rejects_rather_than_grows(self):
        q = WorkQueue(maxsize=2)
        q.submit(tg("a"), user_id="berat")
        q.submit(tg("b"), user_id="berat")
        assert q.submit(tg("c"), user_id="berat").disposition == "rejected"

    def test_closed_queue_rejects(self):
        q = WorkQueue()
        q.close()
        assert q.submit(tg()).disposition == "rejected"

    def test_admission_accepted_property(self):
        q = WorkQueue()
        assert q.submit(tg(), user_id="berat").accepted is True
        assert q.submit(hb()).accepted is True
        q.submit(hb())
        assert q.submit(hb()).accepted is False


class TestQueueUnderAGate:
    """The interesting half: what a queue does while the worker is parked."""

    def _park(self, gate: ChannelGate) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: gate.callback_for("berat", "grp")("apply_change", {"path": "x"}),
            daemon=True,
        )
        thread.start()
        for _ in range(100):
            if gate.pending:
                break
            time.sleep(0.01)
        return thread

    def test_new_request_is_rejected_with_a_reason_not_queued(self):
        """A bounded, explained wait beats a message that appears swallowed."""
        gate = ChannelGate(send=lambda t, x: None, timeout=1.0)
        q = WorkQueue(gate=gate)
        worker = self._park(gate)

        admission = q.submit(tg("unrelated"), user_id="dias")
        assert admission.disposition == "rejected"
        assert admission.reason == GATE_BUSY_REASON
        assert len(q) == 0
        gate.cancel()
        worker.join(2)

    def test_requesters_answer_reaches_the_gate_and_is_not_queued(self):
        """Otherwise the single worker deadlocks against itself."""
        gate = ChannelGate(send=lambda t, x: None, timeout=2.0)
        q = WorkQueue(gate=gate)
        worker = self._park(gate)

        admission = q.submit(tg("y"), user_id="berat")
        assert admission.disposition == "gate_reply"
        assert len(q) == 0
        worker.join(2)
        assert gate.pending is None

    def test_bystander_can_neither_approve_nor_cancel(self):
        gate = ChannelGate(send=lambda t, x: None, timeout=1.0)
        q = WorkQueue(gate=gate)
        worker = self._park(gate)

        assert q.submit(tg("y"), user_id="dias").disposition == "rejected"
        assert gate.pending is not None      # still waiting on berat
        gate.cancel()
        worker.join(2)

    def test_machine_events_still_flow_while_a_gate_is_open(self):
        """Only the interactive path is refused — a push has nobody waiting."""
        gate = ChannelGate(send=lambda t, x: None, timeout=1.0)
        q = WorkQueue(gate=gate)
        worker = self._park(gate)

        assert q.submit(gh("main")).disposition == "queued"
        gate.cancel()
        worker.join(2)


# --- the gate -----------------------------------------------------------------

class TestChannelGate:
    def test_affirmative_approves(self):
        gate = ChannelGate(send=lambda t, x: None, timeout=2.0)
        out: dict = {}
        thread = threading.Thread(
            target=lambda: out.update(ok=gate.callback_for("berat", "grp")("apply_change", {})),
            daemon=True,
        )
        thread.start()
        while not gate.pending:
            time.sleep(0.01)
        assert gate.submit_answer(user_id="berat", thread_key="grp", text="Y")
        thread.join(2)
        assert out["ok"] is True

    @pytest.mark.parametrize("answer", ["n", "no", "maybe", "ok", "sure", "go ahead", ""])
    def test_everything_that_is_not_affirmative_declines(self, answer):
        """An ambiguous approval is a decline. The gate does not classify intent."""
        assert answer.strip().lower() not in AFFIRMATIVE
        gate = ChannelGate(send=lambda t, x: None, timeout=2.0)
        out: dict = {}
        thread = threading.Thread(
            target=lambda: out.update(ok=gate.callback_for("berat", "grp")("apply_change", {})),
            daemon=True,
        )
        thread.start()
        while not gate.pending:
            time.sleep(0.01)
        gate.submit_answer(user_id="berat", thread_key="grp", text=answer)
        thread.join(2)
        assert out["ok"] is False

    def test_timeout_declines(self):
        """An unanswered gate that eventually proceeds is a delay, not a gate."""
        gate = ChannelGate(send=lambda t, x: None, timeout=0.3)
        started = time.time()
        assert gate.callback_for("berat", "grp")("prune_graph_node", {}) is False
        assert time.time() - started >= 0.3
        assert gate.pending is None

    def test_same_user_wrong_thread_does_not_answer(self):
        gate = ChannelGate(send=lambda t, x: None, timeout=1.0)
        thread = threading.Thread(
            target=lambda: gate.callback_for("berat", "grp")("apply_change", {}),
            daemon=True,
        )
        thread.start()
        while not gate.pending:
            time.sleep(0.01)
        assert gate.submit_answer(user_id="berat", thread_key="dm", text="y") is False
        gate.cancel()
        thread.join(2)

    def test_answer_with_no_pending_gate_is_not_consumed(self):
        gate = ChannelGate(send=lambda t, x: None)
        assert gate.submit_answer(user_id="berat", thread_key="grp", text="y") is False

    def test_shutdown_declines_rather_than_approves(self):
        gate = ChannelGate(send=lambda t, x: None, timeout=5.0)
        out: dict = {}
        thread = threading.Thread(
            target=lambda: out.update(ok=gate.callback_for("berat", "grp")("apply_change", {})),
            daemon=True,
        )
        thread.start()
        while not gate.pending:
            time.sleep(0.01)
        gate.cancel()
        thread.join(2)
        assert out["ok"] is False

    def test_file_bodies_are_elided_from_the_prompt(self):
        """An unreadable prompt gets approved reflexively, which is worse than none."""
        body = "x" * 5000
        preview = preview_args("apply_change", {"path": "a.py", "new_content": body})
        assert body not in preview
        assert "5000 chars" in preview

    def test_the_gate_asks_before_it_waits(self):
        sent: list[tuple[str, str]] = []
        gate = ChannelGate(send=lambda t, x: sent.append((t, x)), timeout=0.2)
        gate.callback_for("berat", "grp")("apply_change", {"path": "a.py"})
        assert sent[0][0] == "grp"
        assert "Approval needed" in sent[0][1]
        assert "declined" in sent[-1][1].lower()


# --- the worker ---------------------------------------------------------------

class TestWorker:
    def test_drain_processes_in_order_on_this_thread(self):
        q = WorkQueue()
        for i in range(4):
            q.submit(tg(f"q{i}"), user_id="berat")
        seen: list[str] = []
        assert Worker(q, lambda e: seen.append(e.text)).drain() == 4
        assert seen == ["q0", "q1", "q2", "q3"]

    def test_a_failing_handler_does_not_stop_the_worker(self):
        """One malformed payload must not take every queued question with it."""
        q = WorkQueue()
        q.submit(tg("boom"), user_id="berat")
        q.submit(tg("fine"), user_id="berat")
        seen: list[str] = []
        errors: list[str] = []

        def handler(event):
            if event.text == "boom":
                raise ValueError("bad payload")
            seen.append(event.text)

        worker = Worker(q, handler, on_error=lambda e, exc: errors.append(str(exc)))
        worker.drain()
        assert seen == ["fine"]
        assert errors == ["bad payload"]


# --- silences -----------------------------------------------------------------

class TestSilenceStore:
    def test_a_silence_scoped_to_one_user_is_invisible_to_everyone_else(self):
        """The leak guard is pointless if the audit log announces what it withheld."""
        conn = overlay_db.connect()
        overlay_db.record_silence(
            conn, trigger="telegram", reason_code=REASON_PRIVATE_DECISION,
            evidence="uid=Module:overlay.db candidates=2", visibility="user:berat",
        )
        overlay_db.record_silence(
            conn, trigger="heartbeat", reason_code="heartbeat_clean",
            evidence="graded 3 runs, 0 problems", visibility="team",
        )
        berat = [r["reason_code"] for r in overlay_db.query_silences(conn, user_id="berat")]
        dias = [r["reason_code"] for r in overlay_db.query_silences(conn, user_id="dias")]
        anon = [r["reason_code"] for r in overlay_db.query_silences(conn, user_id="")]
        conn.close()

        assert REASON_PRIVATE_DECISION in berat
        assert dias == ["heartbeat_clean"]
        assert anon == ["heartbeat_clean"]

    def test_counts_use_the_same_filter_as_the_rows(self):
        conn = overlay_db.connect()
        overlay_db.record_silence(conn, trigger="telegram", reason_code="x",
                                  evidence="e", visibility="user:berat")
        overlay_db.record_silence(conn, trigger="telegram", reason_code="y",
                                  evidence="e", visibility="team")
        assert overlay_db.count_silences(conn, user_id="berat") == 2
        assert overlay_db.count_silences(conn, user_id="dias") == 1
        conn.close()

    def test_filtering_by_reason_keeps_the_visibility_filter(self):
        conn = overlay_db.connect()
        overlay_db.record_silence(conn, trigger="telegram",
                                  reason_code=REASON_PRIVATE_DECISION,
                                  evidence="e", visibility="user:berat")
        rows = overlay_db.query_silences(conn, user_id="dias",
                                         reason_code=REASON_PRIVATE_DECISION)
        conn.close()
        assert rows == []

    def test_silences_survive_a_reconnect(self):
        conn = overlay_db.connect()
        overlay_db.record_silence(conn, trigger="heartbeat", reason_code="heartbeat_clean",
                                  evidence="e", visibility="team")
        conn.close()
        conn = overlay_db.connect()
        assert len(overlay_db.query_silences(conn, user_id="berat")) == 1
        conn.close()


class TestSilenceContract:
    """The stub is a contract (T11.2 is Dias's). These pin the shape, not the body."""

    def test_evaluate_silence_is_still_a_stub(self):
        with pytest.raises(NotImplementedError):
            evaluate_silence(tg(), resolve(tg()).session, [])

    def test_speak_is_the_ordinary_outcome(self):
        assert SilenceDecision.speak().silent is False

    def test_reason_codes_are_a_closed_set(self):
        assert is_valid_reason(REASON_PRIVATE_DECISION)
        assert not is_valid_reason("because I felt like it")
        assert REASON_PRIVATE_DECISION in REASON_CODES

    def test_owner_scope_reads_the_owner_off_a_row(self):
        assert owner_scope({"visibility": "user:berat"}) == "user:berat"
        assert owner_scope({"visibility": "team"}) is None


class TestCrossScopeRead:
    def test_decisions_across_scopes_sees_what_the_filter_hides(self):
        """The unfiltered side of the guard's comparison. Its rows never leave it."""
        from agentlib.session import session_scope
        conn = overlay_db.connect()
        with session_scope("berat"):
            overlay_db.insert_decision(
                conn, component="overlay.db", decision="use sqlite",
                rationale="the query is the product", status="accepted",
                author_id="berat", visibility="user:berat",
            )
        uid = "Module:overlay.db"
        filtered = overlay_db.query_decisions(conn, user_id="dias", symbol_uids=[uid])
        unfiltered = overlay_db.decisions_across_scopes(conn, symbol_uids=[uid])
        conn.close()

        assert filtered == []          # what dias may see
        assert len(unfiltered) == 1    # what exists — the comparison T11.2 needs

    def test_empty_uid_list_returns_nothing_rather_than_everything(self):
        """A missing filter must not silently become 'select all'."""
        conn = overlay_db.connect()
        overlay_db.insert_decision(
            conn, component="a.b", decision="d", rationale="r",
            status="accepted", author_id="x", visibility="team",
        )
        assert overlay_db.decisions_across_scopes(conn, symbol_uids=[]) == []
        assert overlay_db.decisions_across_scopes(conn, symbol_uids=["", None]) == []
        conn.close()


# --- telegram: the untrusted-input surface ------------------------------------

class TestTelegramParsing:
    def channel(self, **kwargs) -> TelegramChannel:
        return TelegramChannel(bot_token="test-token", **kwargs)

    def test_a_normal_message_becomes_an_event(self):
        event = self.channel().to_event({
            "update_id": 1,
            "message": {"message_id": 5, "text": "  why sqlite?  ",
                        "chat": {"id": -100, "type": "group"},
                        "from": {"id": 42}},
        })
        assert event.text == "why sqlite?"
        assert event.external_user_id == "42"
        assert event.thread_key == "-100"
        assert event.coalescable is False

    @pytest.mark.parametrize("update", [
        {},
        {"message": {}},
        {"message": {"text": "hi"}},                          # no chat
        {"message": {"chat": {"id": 1}}},                     # no text
        {"message": {"text": "   ", "chat": {"id": 1}}},      # blank text
        {"message": {"text": 42, "chat": {"id": 1}}},         # text not a string
    ])
    def test_malformed_updates_are_ignored_not_raised(self, update):
        """Everything on an update is attacker-controlled in a public group."""
        assert self.channel().to_event(update) is None

    def test_updates_from_other_chats_are_dropped_before_becoming_events(self):
        channel = self.channel(allowed_chats={"-100"})
        assert channel.to_event({"message": {"text": "hi", "chat": {"id": -999},
                                             "from": {"id": 42}}}) is None
        assert channel.to_event({"message": {"text": "hi", "chat": {"id": -100},
                                             "from": {"id": 42}}}) is not None

    def test_a_message_with_no_sender_resolves_anonymous(self, allowlist):
        """Channel posts have no `from`. They must not inherit a session."""
        event = self.channel().to_event(
            {"message": {"text": "hi", "chat": {"id": -100}}}
        )
        assert event.external_user_id is None
        assert resolve(event).known is False

    def test_display_names_are_not_identity(self, allowlist):
        """A user can set their name to anything, including someone else's id."""
        event = self.channel().to_event({
            "message": {"text": "hi", "chat": {"id": -100},
                        "from": {"id": 999, "first_name": "berat",
                                 "username": "berat"}},
        })
        assert resolve(event).session.user_id != "berat"

    def test_a_silent_reply_sends_nothing(self):
        sent: list = []
        channel = self.channel()
        channel._post = lambda *a, **k: sent.append(a)   # type: ignore[assignment]
        channel.send(OutboundReply.quiet("grp"))
        channel.send(OutboundReply(thread_key="grp", text="   "))
        assert sent == []
        channel.send(OutboundReply(thread_key="grp", text="real answer"))
        assert len(sent) == 1

    def test_over_long_replies_are_clipped_not_dropped(self):
        captured: dict = {}
        channel = self.channel()
        channel._post = lambda m, p, timeout: captured.update(p)  # type: ignore[assignment]
        channel.send_text("grp", "x" * 9000)
        assert len(captured["text"]) <= 4000
        assert captured["text"].endswith("(truncated)")

    def test_a_failed_send_does_not_raise(self):
        """Losing one reply beats losing the process that owes replies to everyone."""
        channel = self.channel()

        def boom(*args, **kwargs):
            raise OSError("network is down")

        channel._post = boom  # type: ignore[assignment]
        channel.send_text("grp", "hello")   # must not raise

    def test_an_unconfigured_channel_says_so_instead_of_polling(self):
        channel = TelegramChannel(bot_token="")
        assert channel.configured() is False
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            next(channel.poll())

    def test_the_token_is_not_in_the_repr(self):
        """A traceback in a group chat should not publish the bot's credentials."""
        channel = self.channel()
        assert "test-token" not in repr(channel)


# --- service wiring -----------------------------------------------------------

class TestServiceRegistry:
    def test_the_anonymous_registry_contains_no_write_tools(self):
        """Narrow by construction: built from a list, never filtered from the full set."""
        from service import build_channel_registry

        _, read_only = build_channel_registry(can_write=False)
        assert "append_decision_record" not in read_only
        assert "save_memory" not in read_only
        assert "apply_change" not in read_only
        assert "prune_graph_node" not in read_only
        assert "retrieve_decisions" in read_only

    def test_a_known_user_gains_only_the_two_authoring_tools(self):
        from service import build_channel_registry

        _, read_only = build_channel_registry(can_write=False)
        _, writer = build_channel_registry(can_write=True)
        assert set(writer) - set(read_only) == {"append_decision_record", "save_memory"}

    def test_no_gated_tool_is_ever_in_a_channel_registry(self):
        from agentlib.guards import GATED
        from service import build_channel_registry

        for can_write in (False, True):
            _, registry = build_channel_registry(can_write)
            assert not (set(registry) & GATED)

    def test_every_schema_is_well_formed(self):
        from service import build_channel_registry

        schemas, registry = build_channel_registry(can_write=True)
        assert {s["name"] for s in schemas} == set(registry)
        for schema in schemas:
            assert schema["description"].strip()
            assert "parameters" in schema


class TestSilenceSeam:
    def test_the_service_survives_the_unimplemented_policy(self):
        """T11.2 is not landed yet; the service must degrade loudly, not crash."""
        import service

        service._silence_warned = False
        decision = service.check_silence(tg("why sqlite?"), resolve(tg()))
        assert decision.silent is False

    def test_mentioned_uids_only_matches_components_that_have_decisions(self):
        import service
        from agentlib.session import session_scope

        conn = overlay_db.connect()
        with session_scope("berat"):
            overlay_db.insert_decision(
                conn, component="overlay.db", decision="d", rationale="r",
                status="accepted", author_id="berat", visibility="team",
            )
        hits = service.mentioned_uids(
            "why does overlay.db use sqlite and not tools.graph_query?", conn
        )
        conn.close()
        assert hits == ["Module:overlay.db"]

    def test_mentioned_uids_is_empty_when_nothing_is_recorded(self):
        import service

        conn = overlay_db.connect()
        assert service.mentioned_uids("overlay.db agentlib.core", conn) == []
        conn.close()
