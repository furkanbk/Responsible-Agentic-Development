"""service.py — the long-running process: channel in, agent, channel out.

Owner: Berat Furkan Kocak (HW3, T9.6).

The fifth entry point, and the first one that is not a command somebody ran.
`main.py` and `orchestrator.py` answer one request and exit; this holds a channel
open, establishes identity per event, and runs one turn at a time.

    channel.poll ──> identity.resolve ──> queue.submit ──> [one worker]
                                              │                 │
                                              │                 ├─ silence check
                                              ├─ rejected       ├─ read-only answer
                                              ├─ coalesced      └─ change request
                                              └─ dropped                │
                                                                   gate ──> channel

Two shapes of work, and the split is a permission boundary, not a convenience:

  **A question** (the default) runs `run_agent` over a READ-ONLY registry. It
  cannot write, so it needs no gate and no impact set.

  **A change** (`/change ...`) runs the HW2 planner/executor pipeline through
  `orchestrator.run_change_request`, with the gate wired to the channel. Only an
  allowlisted user can start one — `Identity.can_write`.

The registries are built from explicit lists (`_READ_TOOLS`, `_WRITE_TOOLS`), not
by filtering the full registry. A filter is one bug away from being a full
registry; a list is not. This is the same construction the executor uses (#31),
and the admin path in T11.3 follows it too.

Nothing in this file decides what the agent may SEE. That is still the query's
job (decision #24): `session_scope` sets who is asking and `visible_to` does the
rest, so a bug here can lose an answer but cannot leak one.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Any, Callable, Iterable, Optional

from dotenv import load_dotenv

from agentlib.approval import ChannelGate
from agentlib.context import assemble
from agentlib.core import CHEAP, STRONG
from agentlib.loop import DEFAULT_SYSTEM, run_agent
from agentlib.runlog import RunLog
from agentlib.schemas import schema_for
from agentlib.session import session_scope
from channel.base import InboundEvent, OutboundReply
from channel.identity import Identity, resolve
from channel.queue import Admission, WorkQueue, Worker
from channel.silence import SilenceDecision, evaluate_silence
from channel.telegram import TelegramChannel
from orchestrator import run_change_request
from overlay import db as overlay_db
from tracing import init_tracing, request_origin_scope
from overlay.uid import resolve_uid
from tools.decisions import append_decision_record, retrieve_decisions, verify_graph_integrity
from tools.graph_query import query_component_graph
from tools.memory_tools import retrieve_memory, save_memory
from tools.retrieval_tools import search_corpus

CHANGE_PREFIX = "/change"
MAX_STEPS = 8

# Narrow by construction. Anyone may read the team's published knowledge.
#
# `search_corpus` leads, matching `tools/__init__.py`'s ordering and for its
# reason: it is the only one of the four that takes a question phrased the way
# people phrase them in a chat window — "which bit handles the approval prompt" —
# and returns something to look up. The two exact lookups then take over. Without
# it the channel path could only answer questions that already named a component,
# which is most of what a stranger asks and none of how they ask it.
#
# Safe on the anonymous path for the same reason the others are: visibility is a
# `WHERE` clause (#24) — `retrieval.store._visibility_clause` mirrors
# `overlay.db.visible_to` against the ambient `current_user()`, on BOTH the dense
# and the lexical arm — not an instruction asking the model to skip rows. And it
# is read-only, so it stays ungated (CLAUDE.md §5).
_READ_TOOLS = (search_corpus, query_component_graph, retrieve_decisions,
               retrieve_memory, verify_graph_integrity)
# Additionally available to an allowlisted user. Still no `apply_change` and no
# `prune_graph_node` — those belong to the change pipeline and to T11.3.
_WRITE_TOOLS = (append_decision_record, save_memory)

HELP_TEXT = (
    "I answer questions about this codebase from its decision overlay.\n"
    "  ask anything          — a read-only question over the knowledge graph\n"
    f"  {CHANGE_PREFIX} <request>      — plan and implement a change (allowlisted users)\n"
    "  /whoami               — how I resolved your identity\n"
    "  /help                 — this message"
)


def build_channel_registry(can_write: bool) -> tuple[list[dict], dict[str, Callable]]:
    """The toolset for one turn, assembled from an explicit list.

    `can_write=False` is the anonymous path: read the team's published knowledge,
    add nothing to it. That is enforced twice on purpose — the tools are absent
    here, and `append_decision_record` independently refuses a falsy author with
    `no_session` (see `channel.identity`).
    """
    functions = list(_READ_TOOLS) + (list(_WRITE_TOOLS) if can_write else [])
    schemas = [schema_for(fn) for fn in functions]
    registry = {fn.__name__: fn for fn in functions}
    return schemas, registry


# --- the silence seam ---------------------------------------------------------

_silence_warned = False


def mentioned_uids(text: str, conn: Any) -> list[str]:
    """Which components with decisions on file this message refers to.

    Code-owned and mechanical, in the spirit of decision #27: tokens are
    normalised through `resolve_uid` and intersected with the uids that actually
    carry decisions. No model call decides what a question is about, so a wrong
    answer here is traceable to a token, never to model vibes.
    """
    known = set(overlay_db.all_decision_uids(conn))
    if not known:
        return []
    hits: list[str] = []
    for raw in (text or "").replace("(", " ").replace(")", " ").replace(",", " ").split():
        token = raw.strip(".:;!?\"'`").strip()
        if len(token) < 4:
            continue
        uid = resolve_uid(token)
        if uid in known and uid not in hits:
            hits.append(uid)
    return hits


def check_silence(event: InboundEvent, identity: Identity) -> SilenceDecision:
    """Ask the silence policy whether this event must go unanswered.

    The policy itself is Dias's T11.2. Until it lands this returns "speak" and
    says so once — loudly, because a silence guard that is quietly absent is
    worse than one that is loudly absent, and the whole point of the branch is
    that its failure mode is invisible by construction.
    """
    global _silence_warned
    conn = overlay_db.connect()
    try:
        uids = mentioned_uids(event.text, conn)
        # The unfiltered side of the comparison. These rows do not leave
        # `evaluate_silence` — see `overlay.db.decisions_across_scopes`.
        candidates = overlay_db.decisions_across_scopes(conn, symbol_uids=uids)
    finally:
        conn.close()

    try:
        return evaluate_silence(event, identity.session, candidates)
    except NotImplementedError:
        if not _silence_warned:
            print("  [silence] policy not implemented yet (T11.2, Dias) — "
                  "answering normally. The leak guard is NOT active.")
            _silence_warned = True
        return SilenceDecision.speak()


def record_silence(decision: SilenceDecision, event: InboundEvent,
                   run_id: Optional[str] = None) -> None:
    """Persist a deliberate non-answer. Silence without a reason on file is an outage."""
    conn = overlay_db.connect()
    try:
        overlay_db.record_silence(
            conn, trigger=event.source, reason_code=decision.reason_code,
            evidence=decision.evidence, visibility=decision.visibility,
            run_id=run_id,
        )
    finally:
        conn.close()


# --- the two work shapes ------------------------------------------------------

def answer_question(event: InboundEvent, identity: Identity, *, model: str,
                    verbose: bool) -> str:
    """Read-only turn. No gate, because nothing here can write."""
    schemas, registry = build_channel_registry(identity.can_write)
    conn = overlay_db.connect()
    try:
        impact = mentioned_uids(event.text, conn)
    finally:
        conn.close()

    context = assemble(
        base_system=DEFAULT_SYSTEM, query=event.text,
        impact=impact, session=identity.session,
    )
    run_log = RunLog(
        agent=f"channel:{event.source}", user_id=identity.session.user_id,
        thread_id=identity.session.thread_id, request=event.text,
    )
    result = run_agent(
        event.text, schemas, registry,
        approve=None,          # nothing in this registry is gated
        model=model, max_steps=MAX_STEPS, verbose=verbose,
        context=context, run_log=run_log,
    )
    answer = result.get("answer")
    if answer:
        return answer
    return (f"I stopped without an answer ({result['stopped']}). "
            f"Run id {result.get('run_id')} if you want to look.")


def apply_change_request(event: InboundEvent, identity: Identity, gate: ChannelGate,
                         *, model: str, verbose: bool) -> str:
    """The HW2 pipeline, with the gate answered over the channel instead of stdin."""
    request = event.text[len(CHANGE_PREFIX):].strip()
    if not request:
        return f"Usage: {CHANGE_PREFIX} <what you want changed>"
    if not identity.can_write:
        return ("I only take change requests from allowlisted users, and I don't "
                "recognise you. Ask a maintainer to add your id.")

    result = run_change_request(
        request,
        user_id=identity.session.user_id,
        thread_id=identity.session.thread_id,
        approve=gate.callback_for(identity.session.user_id, event.thread_key),
        model=model,
        verbose=verbose,
    )
    changed = result.get("changes") or []
    lines = [f"status: {result['status']}", f"run id: {result['run_id']}"]
    if changed:
        lines.append(f"changed {len(changed)} file(s): "
                     + ", ".join(str(c.get('path')) for c in changed))
    if result.get("notes"):
        lines.append(result["notes"])
    return "\n".join(lines)


# --- the handler --------------------------------------------------------------

def make_handler(send: Callable[[str, str], None], gate: ChannelGate, *,
                 model: str, verbose: bool) -> Callable[[InboundEvent], None]:
    """Build the worker's handler. One turn, start to finish, on one thread."""

    def handle(event: InboundEvent) -> None:
        # HW6 (T15.5): `request_origin` is stamped HERE, at the one place every
        # inbound event crosses into the system, from the source the runtime
        # observed — never from an argument and never from anything the model or
        # the sender can influence (#25). A github push is machine traffic
        # (`api`); a person in a chat is `ui`. `triggers/webhook.py` and
        # `triggers/heartbeat.py` belong to other owners and need no edit: the
        # webhook's events arrive through this handler, and the heartbeat sets
        # its own `batch` scope in T15.20.
        with request_origin_scope("api" if event.source == "github" else "ui"):
            _handle(event)

    def _handle(event: InboundEvent) -> None:
        # A github event is not a chat turn: no human is waiting, `text` is empty,
        # and the work is a rescan + orphan diff (T10.2), not a `run_agent`
        # answer. Route it to the watcher before the Q&A path below would mistake
        # it for an empty question.
        if event.source == "github":
            from triggers import orphan_watch
            orphan_watch.handle_github_event(event, send=send, verbose=verbose)
            return

        identity = resolve(event)
        if verbose:
            print(f"\n[{event.source}] {identity} — {event.text[:80]!r}")

        with session_scope(identity.session.user_id, identity.session.thread_id):
            text = (event.text or "").strip()

            if text in ("/help", "/start"):
                send(event.thread_key, HELP_TEXT)
                return
            if text == "/whoami":
                # The external id is echoed because it is the one thing an
                # operator cannot look up from outside: it is what goes in
                # RADF_CHANNEL_USERS to map this person to a RADF user. Safe to
                # show — it is the sender's own id, told back to the sender.
                send(event.thread_key,
                     f"You resolve to: {identity}\n"
                     f"  telegram id : {identity.external_id or '(none)'}\n"
                     f"  chat id     : {event.thread_key}\n"
                     f"  known to me : {identity.known}\n"
                     f"  may write   : {identity.can_write}\n"
                     f"  admin       : {identity.is_admin}\n"
                     + ("Unrecognised senders read team-visible records only. To be "
                        f"recognised, add  telegram:{identity.external_id}=<your-name>  "
                        "to RADF_CHANNEL_USERS in .env and restart me."
                        if not identity.known else
                        "You are on the allowlist."))
                return

            silence = check_silence(event, identity)
            if silence.silent:
                record_silence(silence, event)
                if verbose:
                    print(f"  [SILENT] {silence.reason_code} — recorded, nothing sent")
                return  # deliberately no reply. See channel/silence.py.

            if text.startswith(CHANGE_PREFIX):
                reply = apply_change_request(event, identity, gate,
                                             model=model, verbose=verbose)
            else:
                reply = answer_question(event, identity, model=model, verbose=verbose)

        send(event.thread_key, reply)

    return handle


def on_handler_error(event: InboundEvent, exc: BaseException) -> None:
    """A failed turn is reported and survived. One bad event does not end the service."""
    print(f"  [ERROR] handling {event.source} event failed: {exc!r}", file=sys.stderr)


# --- the reader ---------------------------------------------------------------

def pump(channel: Any, queue: WorkQueue, send: Callable[[str, str], None],
         *, verbose: bool) -> None:
    """Read a channel forever and submit what it yields.

    The reader resolves identity for the queue's benefit only — the handler
    resolves it again for real. Doing it twice is cheap and keeps the queue from
    depending on a handler-side decision.
    """
    for event in channel.poll():
        identity = resolve(event)
        admission = queue.submit(event, user_id=identity.session.user_id)
        if verbose and admission.disposition != "queued":
            print(f"  [QUEUE] {event.source}: {admission.disposition}"
                  + (f" — {admission.reason}" if admission.reason else ""))
        # Only a human is told they were turned away; a coalesced webhook has
        # nobody waiting on it.
        if admission.disposition == "rejected" and event.interactive:
            send(event.thread_key, admission.reason)


# --- dry run ------------------------------------------------------------------

class ScriptedChannel:
    """A channel backed by a list, for `--dry-run`.

    Same protocol, same queue, same handler, same ordering — no network and no
    model. This is what makes the queue's policy testable as a policy rather
    than as a thing that seems to work when you watch it.
    """

    name = "scripted"

    def __init__(self, events: Iterable[InboundEvent]) -> None:
        self._events = list(events)
        self.sent: list[OutboundReply] = []

    def poll(self):
        yield from self._events

    def send(self, reply: OutboundReply) -> None:
        if reply.silent:
            return
        self.sent.append(reply)

    def send_text(self, thread_key: str, text: str) -> None:
        self.send(OutboundReply(thread_key=thread_key, text=text))


def dry_run(verbose: bool = True) -> int:
    """Exercise admission and ordering with no network and no model.

    Deliberately does NOT run the agent: this proves the queue's policy, and
    mixing a live model into that would make a policy failure look like a model
    failure.
    """
    gate = ChannelGate(send=lambda thread, text: print(f"  [gate->{thread}] {text}"))
    queue = WorkQueue(gate=gate)

    events = [
        InboundEvent(source="telegram", thread_key="grp", text="why is the overlay sqlite?",
                     external_user_id="42"),
        InboundEvent(source="github", thread_key="team", dedupe_key="push:main"),
        InboundEvent(source="github", thread_key="team", dedupe_key="push:main"),
        InboundEvent(source="github", thread_key="team", dedupe_key="push:dev"),
        InboundEvent(source="heartbeat", thread_key="team"),
        InboundEvent(source="heartbeat", thread_key="team"),
        InboundEvent(source="telegram", thread_key="grp", text="second question",
                     external_user_id="42"),
    ]

    print("submitting 7 events (2 duplicate pushes, 2 heartbeats):\n")
    for event in events:
        identity = resolve(event)
        admission = queue.submit(event, user_id=identity.session.user_id)
        print(f"  {event.source:9} {(event.dedupe_key or event.text)[:34]:36} "
              f"-> {admission.disposition}"
              + (f"  ({admission.reason})" if admission.reason else ""))

    print(f"\nqueue order: {queue.pending_sources()}")
    print(f"stats      : {queue.stats}")

    handled: list[str] = []
    worker = Worker(queue, lambda e: handled.append(f"{e.source}:{e.dedupe_key or e.text[:20]}"))
    worker.drain()
    print(f"\ndrained in order:")
    for item in handled:
        print(f"  {item}")
    print("\nFIFO preserved; duplicates collapsed before they reached the worker.")
    return 0


# --- entry point --------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RADF HW3 — the agent as a channel bot."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="exercise the queue with a scripted channel, no network")
    # `strong` by default (decision #76): the cheap model's failure modes on this
    # workload are parse-level, not quality-level — a duplicated plan object, an
    # empty seed — and those surface as "the planner failed", which reads as a
    # broken system rather than a cheap one. `--model cheap` is still there.
    parser.add_argument("--model", choices=["cheap", "strong"], default="strong")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--chats", default="",
                        help="comma-separated chat ids to serve (default: any)")
    parser.add_argument("--gate-timeout", type=float, default=180.0,
                        help="seconds before an unanswered approval declines")
    parser.add_argument("--trace", action="store_true",
                        help="record MLflow traces for every turn (HW6). Off by "
                             "default: tracing is opt-in per process, so a "
                             "service run is never blocked by a trace backend.")
    args = parser.parse_args(argv)

    load_dotenv()
    verbose = not args.quiet
    if args.trace and not init_tracing():
        print("  [TRACE] mlflow unavailable — running untraced", file=sys.stderr)

    if args.dry_run:
        return dry_run(verbose)

    channel = TelegramChannel(
        allowed_chats={c.strip() for c in args.chats.split(",") if c.strip()} or None
    )
    if not channel.configured():
        print("TELEGRAM_BOT_TOKEN is not set. Create a fresh bot with @BotFather "
              "and put its token in .env — never a personal account's.",
              file=sys.stderr)
        return 2

    try:
        me = channel.whoami()
        print(f"[service] connected as @{me.get('username')} (id {me.get('id')})")
    except Exception as exc:  # noqa: BLE001 — a bad token should say so, here
        print(f"[service] could not reach Telegram: {exc}", file=sys.stderr)
        return 2

    gate = ChannelGate(send=channel.send_text, timeout=args.gate_timeout)
    queue = WorkQueue(gate=gate)
    handler = make_handler(channel.send_text, gate,
                           model=STRONG if args.model == "strong" else CHEAP,
                           verbose=verbose)
    worker = Worker(queue, handler, on_error=on_handler_error)
    worker.start()

    reader = threading.Thread(
        target=pump, args=(channel, queue, channel.send_text),
        kwargs={"verbose": verbose}, name="radf-reader", daemon=True,
    )
    reader.start()
    print("[service] listening. Ctrl-C to stop.")

    try:
        while reader.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[service] shutting down")
    finally:
        # A pending gate is abandoned as DECLINED, never as approved: shutdown
        # is not consent.
        gate.cancel()
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
