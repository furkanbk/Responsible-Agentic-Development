"""channel.queue — what happens when something arrives mid-turn.

Owner: Berat Furkan Kocak (HW3, T9.3).

The four options are drop, queue, interrupt, and coalesce. This system uses
**one worker, FIFO, with a per-path admission policy**, because the three paths
into it want genuinely different things and forcing one strategy on all of them
would be a worse answer for at least two.

Why one worker
--------------
Not throughput laziness — a correctness constraint. Identity and write scope are
ambient, held in `contextvars` (decision #25): `session_scope` and `impact_scope`
are what keep A's private rows away from B and what authorise a file write. Two
turns running concurrently in one process would each need their own context, and
the failure mode of getting that wrong is not a slow response, it is user B's
agent acting as user A. A worker pool would trade that boundary for throughput
this system does not need. One worker makes the race structurally impossible
rather than carefully avoided.

The three paths
---------------
**Webhook → coalesce.** Ten pushes to one branch produce ten "re-scan and check
for orphans" events with identical outcomes. Collapsing them to the newest
waiting one is correct, not lossy — the scan reads the working tree, so the last
event's answer is the only one that was ever going to be right.
*What it costs:* per-commit granularity. After coalescing, the system can say a
decision went stale in this batch and not which commit did it.

**Heartbeat → drop the duplicate.** If a heartbeat is already waiting, a second
one has nothing to add: the pending one will grade everything unjudged, including
whatever arrived since. Note this keeps the OLDER event where coalescing keeps
the newer — for a job defined by "process everything outstanding" the queued one
is already the complete job.
*What it costs:* nothing, which is why it is a different rule from coalescing.

**Human → queue, except while a gate is open.** A person's message is never
coalesced and never dropped: discarding a question somebody typed is data loss
dressed up as a policy. The one exception is while the worker is parked on an
approval, where a new request is **rejected with a reason** rather than silently
queued behind an unbounded wait.
*What it costs:* the rejected message has to be re-sent by the human. That is a
real cost, and it buys a bounded, explained wait instead of a message that
appears to have been swallowed.

The gate's answer is not a request
----------------------------------
While an approval is pending, a message from the requester in that thread is the
ANSWER, and it must reach the parked gate rather than queue behind the turn that
is waiting for it — which would deadlock the single worker against itself. The
queue offers every interactive message to the gate first; if the gate consumes
it, admission is `gate_reply` and nothing is enqueued.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, Protocol

from .base import InboundEvent

Disposition = Literal["queued", "coalesced", "dropped", "rejected", "gate_reply"]

GATE_BUSY_REASON = (
    "I'm parked on an approval right now and can only run one turn at a time. "
    "Answer the pending request (or let it time out), then send this again."
)


class GateLike(Protocol):
    """The slice of `agentlib.approval.ChannelGate` the queue depends on.

    A Protocol rather than an import so the queue does not depend on the gate's
    module — the queue's job is admission, and it needs exactly two facts about
    the gate: is one open, and does this message answer it.
    """

    def submit_answer(self, *, user_id: str, thread_key: str, text: str) -> bool: ...

    @property
    def pending(self) -> Any: ...


@dataclass(frozen=True)
class Admission:
    """What the queue did with a submitted event, and why.

    Returned rather than raised because none of these are errors. A coalesced
    push and a rejected message are both the policy working; the caller needs to
    know which so it can tell the human, and the write-up needs it so the costs
    above are observable rather than asserted.
    """

    disposition: Disposition
    reason: str = ""

    @property
    def accepted(self) -> bool:
        """True iff this event will be worked on as its own turn."""
        return self.disposition in ("queued", "coalesced")


class WorkQueue:
    """A bounded FIFO with per-source admission. Thread-safe.

    The queue holds policy, not behaviour: it decides what gets in and in what
    order. Running the work is `Worker`'s job, and deciding what the work means
    is `service.py`'s.
    """

    def __init__(self, gate: Optional[GateLike] = None, maxsize: int = 256) -> None:
        self._items: deque[InboundEvent] = deque()
        self._cond = threading.Condition()
        self._gate = gate
        self._maxsize = maxsize
        self._closed = False
        self.stats: dict[str, int] = {
            "queued": 0, "coalesced": 0, "dropped": 0,
            "rejected": 0, "gate_reply": 0,
        }

    # --- admission ------------------------------------------------------------

    def submit(self, event: InboundEvent, *, user_id: str = "") -> Admission:
        """Offer an event to the queue. Returns what the policy did with it.

        `user_id` is the RESOLVED RADF identity (from `channel.identity`), not
        the platform id — the gate matches on it, and matching a gate answer
        against an unresolved external id would let an unmapped stranger answer
        a mapped user's approval.
        """
        # A gate answer is not a request, and must not queue behind the turn
        # that is blocked waiting for it.
        if event.interactive and self._gate is not None:
            consumed = self._gate.submit_answer(
                user_id=user_id, thread_key=event.thread_key, text=event.text
            )
            if consumed:
                return self._tally(Admission("gate_reply", "answered a pending gate"))

        with self._cond:
            if self._closed:
                return self._tally(Admission("rejected", "the service is shutting down"))

            # Human path: refuse rather than silently queue behind a gate.
            if event.interactive and self._gate is not None and self._gate.pending:
                return self._tally(Admission("rejected", GATE_BUSY_REASON))

            if len(self._items) >= self._maxsize:
                return self._tally(Admission("rejected", "the queue is full"))

            # Heartbeat: the pending one already covers everything outstanding.
            if event.source == "heartbeat":
                if any(e.source == "heartbeat" for e in self._items):
                    return self._tally(
                        Admission("dropped", "a heartbeat is already queued")
                    )

            # Coalescable (webhook): newest wins, position preserved so one
            # busy branch cannot starve another thread's turn by refreshing.
            if event.coalescable:
                for i, waiting in enumerate(self._items):
                    if waiting.dedupe_key == event.dedupe_key:
                        self._items[i] = event
                        self._cond.notify()
                        return self._tally(
                            Admission("coalesced",
                                      f"merged into a waiting {event.dedupe_key!r}")
                        )

            self._items.append(event)
            self._cond.notify()
            return self._tally(Admission("queued"))

    def _tally(self, admission: Admission) -> Admission:
        self.stats[admission.disposition] = self.stats.get(admission.disposition, 0) + 1
        return admission

    # --- consumption ----------------------------------------------------------

    def take(self, timeout: Optional[float] = None) -> Optional[InboundEvent]:
        """Pop the next event, waiting up to `timeout`. None if nothing arrived."""
        with self._cond:
            if not self._items:
                self._cond.wait(timeout)
            if not self._items:
                return None
            return self._items.popleft()

    def close(self) -> None:
        """Stop accepting work and wake anything waiting."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)

    def pending_sources(self) -> list[str]:
        """Sources currently waiting, oldest first — for `--dry-run` and tests."""
        with self._cond:
            return [e.source for e in self._items]


class Worker:
    """The single consumer. Runs `handler(event)` one event at a time.

    A handler exception is caught and reported, never allowed to kill the
    thread: one malformed webhook payload must not end the service and take
    every queued human question with it.
    """

    def __init__(
        self,
        queue: WorkQueue,
        handler: Callable[[InboundEvent], None],
        *,
        on_error: Optional[Callable[[InboundEvent, BaseException], None]] = None,
        poll_seconds: float = 0.25,
    ) -> None:
        self._queue = queue
        self._handler = handler
        self._on_error = on_error
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.handled = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="radf-worker", daemon=True)
        self._thread.start()

    def run(self) -> None:
        """The loop. Call directly to run the worker on the current thread."""
        while not self._stop.is_set():
            event = self._queue.take(timeout=self._poll)
            if event is None:
                continue
            try:
                self._handler(event)
                self.handled += 1
            except BaseException as exc:  # noqa: BLE001 — see the class docstring
                if self._on_error is not None:
                    self._on_error(event, exc)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.close()
        if self._thread is not None:
            self._thread.join(timeout)

    def drain(self, limit: int = 1000) -> int:
        """Process everything currently queued on THIS thread, then return.

        The `--dry-run` and test entry point: same admission policy, same
        handler, same ordering, no thread and no clock.
        """
        done = 0
        while done < limit:
            event = self._queue.take(timeout=0)
            if event is None:
                break
            try:
                self._handler(event)
                self.handled += 1
            except BaseException as exc:  # noqa: BLE001
                if self._on_error is not None:
                    self._on_error(event, exc)
            done += 1
        return done
