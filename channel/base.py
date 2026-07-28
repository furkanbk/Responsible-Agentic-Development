"""channel.base — the one shape every trigger emits.

Owner: Berat Furkan Kocak (HW3, T9.0a).

HW3 gives the system three ways in — a person typing in Telegram, GitHub pushing
a commit, a heartbeat firing on its own clock — and exactly one thing downstream
of them. The queue, the worker and the session bridge know nothing about where an
event came from beyond the `source` tag; everything source-specific stays in the
adapter that built it.

That is not tidiness. Ambient identity (`session_scope`) has to be established
once, in one place, from a field whose meaning is the same regardless of which
adapter filled it in. Three inbound shapes would mean three places that decide
who the acting user is, and the third one is where the bug lives.

**Everything on an InboundEvent is untrusted.** `text` obviously, but also the
display name, the thread key and every value in `payload`. None of it reaches
`instructions` — it goes into `input[]` as quoted data (decision #26) or it is
used as an opaque routing key. A channel is an internet-facing surface; the only
field the system may trust is the one it computed itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Optional, Protocol, runtime_checkable

Source = Literal["telegram", "github", "heartbeat"]

#: Sources that carry a human asking for something, as opposed to the world
#: changing on its own. The queue treats the two differently (T9.3): a person is
#: waiting for an answer, a machine event is not.
INTERACTIVE_SOURCES: frozenset[str] = frozenset({"telegram"})


@dataclass(frozen=True)
class InboundEvent:
    """One thing that happened, from any source, in the shape the worker reads.

    Attributes:
      source            which adapter produced this.
      external_user_id  the channel's own id for the human (a Telegram user id),
                        or None when no human asked — github and heartbeat
                        events have no author, and `None` is what makes them
                        resolve to the low-trust anonymous identity in T9.2
                        rather than inheriting somebody's session.
      thread_key        where a reply goes. Also the queue's ordering key, so
                        one thread's backlog never reorders another's.
      text              the human-readable request. Untrusted, always.
      payload           source-specific data, passed through untouched. The
                        worker does not read it; the trigger that made the event
                        and the handler that consumes it agree on the contents.
      ts                unix time the event was received.
      dedupe_key        set means this event is COALESCABLE: a newer event with
                        the same key may replace it while it waits. `None` means
                        it must not be merged with anything. A human's message
                        is never coalescable — dropping a question somebody
                        typed is not a queueing policy, it is data loss.
    """

    source: Source
    thread_key: str
    text: str = ""
    external_user_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    dedupe_key: Optional[str] = None

    @property
    def interactive(self) -> bool:
        """True when a human is waiting on the other end of this event."""
        return self.source in INTERACTIVE_SOURCES

    @property
    def coalescable(self) -> bool:
        """True when a newer event may replace this one in the queue."""
        return self.dedupe_key is not None


@dataclass(frozen=True)
class OutboundReply:
    """What the worker hands back to a channel.

    `silent=True` is a real, expressible outcome and not an empty string: the
    worker decided to say nothing, the channel honours it by sending nothing,
    and the reason is already recorded in the `silences` table (T9.4). Making
    silence a value rather than an absence is what stops it from looking like a
    crash in the logs.
    """

    thread_key: str
    text: str = ""
    silent: bool = False

    @classmethod
    def quiet(cls, thread_key: str) -> "OutboundReply":
        """A deliberate non-answer. See `channel.silence.evaluate_silence`."""
        return cls(thread_key=thread_key, text="", silent=True)


@runtime_checkable
class Channel(Protocol):
    """A surface the agent reads from and answers on.

    Two methods on purpose. A channel is an adapter, not a framework: it turns
    whatever the platform gives it into `InboundEvent`s and turns
    `OutboundReply`s into whatever the platform wants. It runs no agent, opens
    no store, and makes no decision about whether to answer.
    """

    name: str

    def poll(self) -> Iterator[InboundEvent]:
        """Yield events as they arrive. May block between them.

        Implementations must not raise on a transport failure — a channel that
        dies on one bad gateway is not a channel. Back off and keep yielding.
        """
        ...

    def send(self, reply: OutboundReply) -> None:
        """Deliver a reply. A `silent` reply must send nothing at all."""
        ...
