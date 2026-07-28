"""triggers — the things that start a run when nobody typed anything.

Owner of this file: Berat Furkan Kocak (HW3, T9.0c).
Owner of `webhook.py` / `orphan_watch.py`: Alejandro (Phase 10).
Owner of `heartbeat.py`: Dias (Phase 11).

HW1 and HW2 both started at a CLI prompt, which meant a human had already
decided there was something to do. The interesting failures do not announce
themselves that way: a decision goes stale when somebody moves the file it
points at, and a run misbehaves at 3am. Both need something watching.

Two kinds, and the split is the point:

    webhook.py + orphan_watch.py   an EXTERNAL EVENT — the repo changed under
                                   the records. Interactive-adjacent: it has a
                                   cause, and the cause is somebody's push.

    heartbeat.py                   a CLOCK — nothing happened, which is exactly
                                   when nobody looks. Background: no cause, no
                                   requester, no one waiting.

There is deliberately no trigger registry here. Three triggers, each constructing
an `InboundEvent` and handing it to the queue, is less machinery than a plugin
system and has the same effect — and a registry would be the fourth place that
could decide who the acting user is (see `channel.base`).

A trigger's job ends at "enqueue an event". None of them runs an agent, opens a
store, or answers anything; that would put agent work on an HTTP thread, where
an unauthenticated sender gets to pick how much of it happens.
"""

from __future__ import annotations

from channel.base import InboundEvent, OutboundReply, Source

__all__ = ["InboundEvent", "OutboundReply", "Source"]
