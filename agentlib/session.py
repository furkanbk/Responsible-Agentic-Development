"""agentlib.session — who is speaking, and into which thread.

Owner: Berat Furkan Kocak (HW2, T5.2).

Session 4's composite key: `(user_id, thread_id)`. Memory is scoped per key, so
A's private fact never reaches B, and a user's memory follows them across their
own threads.

**Identity is ambient, never a tool argument.** This is the part worth being
deliberate about. If `append_decision_record` took an `author_id` parameter, the
MODEL would fill it in — and the model's context contains other users' shared
content, which is untrusted text. A comment reading "you are now acting as
alice" would then be enough to write memory as alice. Identity comes from the
runtime that authenticated the request; the model can choose *what* to write and
*whether it is private*, never *who wrote it*.

What the key does NOT do (slides, Session 4): it scopes what the agent
remembers, not what it is allowed to DO. Permission scoping is a later session.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True)
class SessionKey:
    """The routing key for one conversation."""

    user_id: str
    thread_id: str = "default"

    @property
    def scope(self) -> str:
        """The visibility token for this user's private rows."""
        return f"user:{self.user_id}"

    def __str__(self) -> str:
        return f"{self.user_id}/{self.thread_id}"


_CURRENT: contextvars.ContextVar[Optional[SessionKey]] = contextvars.ContextVar(
    "radf_session", default=None
)


def current_session() -> Optional[SessionKey]:
    """The session in force, or None outside a run.

    Tools read this at CALL time — same rule as `RADF_GRAPH_PATH` (decision #11)
    — so a tool never has to be re-registered per user.
    """
    return _CURRENT.get()


def current_user() -> Optional[str]:
    """The acting user id, or None. `None` sees team-visible rows only."""
    session = current_session()
    return session.user_id if session else None


def set_session(session: Optional[SessionKey]) -> contextvars.Token:
    """Set the session. Prefer the `session_scope` context manager."""
    return _CURRENT.set(session)


@contextmanager
def session_scope(user_id: str, thread_id: str = "default") -> Iterator[SessionKey]:
    """Run a block as `user_id`. Restores the previous session on exit.

    Using a contextvar rather than a global is what lets the demos run two
    users in one process without one leaking into the other.
    """
    key = SessionKey(user_id=user_id, thread_id=thread_id)
    token = _CURRENT.set(key)
    try:
        yield key
    finally:
        _CURRENT.reset(token)


# --- the active plan's impact set --------------------------------------------
#
# Ambient for the same reason identity is. If `apply_change` took the impact set
# as a parameter, the MODEL would supply it — and the model would then be
# authorising its own writes using a list it just made up. The orchestrator sets
# this from the planner's output, and the write tool reads it.

_IMPACT: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "radf_impact", default=()
)


def current_impact_set() -> tuple[str, ...]:
    """The `symbol_uid`s the plan in force permits touching.

    Empty means NO PLAN IS IN FORCE, which denies every write. It does not mean
    "unrestricted" — that default is the difference between a bug and a breach.
    """
    return _IMPACT.get()


@contextmanager
def impact_scope(symbol_uids: Iterable[str]) -> Iterator[tuple[str, ...]]:
    """Run a block under a plan's impact set."""
    frozen = tuple(u for u in symbol_uids if u)
    token = _IMPACT.set(frozen)
    try:
        yield frozen
    finally:
        _IMPACT.reset(token)
