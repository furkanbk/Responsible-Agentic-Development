"""channel — the surfaces the agent reads from and answers on.

Owner: Berat Furkan Kocak (HW3, Phase 9), except `silence.evaluate_silence`
(Dias, T11.2).

    base.py      the one inbound/outbound shape everything else speaks
    identity.py  external channel id -> SessionKey (ambient identity, #25)
    queue.py     one worker, FIFO, per-path admission policy
    silence.py   the deliberate non-answer, and its recorded reason
    telegram.py  the interactive surface

Nothing in here calls a model or opens a store. A channel adapts a transport;
deciding what to do with an event is `service.py`'s job, and deciding what the
agent may see is still the query's job (decision #24).

Everything arriving through this package is untrusted input, including fields
that look structural. See `base.InboundEvent`.
"""

from __future__ import annotations

from .base import (
    Channel,
    InboundEvent,
    OutboundReply,
    INTERACTIVE_SOURCES,
)
from .silence import (
    REASON_CODES,
    REASON_HEARTBEAT_CLEAN,
    REASON_INJECTION_ATTEMPT,
    REASON_NO_DECISIONS_TOUCHED,
    REASON_PRIVATE_DECISION,
    SilenceDecision,
    evaluate_silence,
    is_valid_reason,
    owner_scope,
)

__all__ = [
    "Channel",
    "InboundEvent",
    "OutboundReply",
    "INTERACTIVE_SOURCES",
    "SilenceDecision",
    "evaluate_silence",
    "is_valid_reason",
    "owner_scope",
    "REASON_CODES",
    "REASON_HEARTBEAT_CLEAN",
    "REASON_PRIVATE_DECISION",
    "REASON_NO_DECISIONS_TOUCHED",
    "REASON_INJECTION_ATTEMPT",
]
