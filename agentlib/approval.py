"""agentlib.approval — the human gate, moved off stdin.

Owner: Berat Furkan Kocak (HW3, T9.5).

HW1 and HW2 gate irreversible tools with `input()` (decision #3, then T7.1). That
works because the human who typed the request is sitting at the terminal the loop
is blocked on. In HW3 the requester is in Telegram and the loop is on a worker
thread, so `input()` would read from a stdin nobody is attached to and hang the
only worker the system has.

**This is the one HW2 contract HW3 changes, and it is a decision, not a
refactor.** What changes is where the answer comes from. What does not change:
`GATED` in `agentlib.guards` is untouched, the loop's gate branch is untouched,
the callback signature `(name, args) -> bool` is untouched, and `main.py` keeps
`approve_via_input` exactly as it was — so every HW1/HW2 test still exercises the
gate it was written against.

Three properties worth stating, because each is a way this could have been wrong:

**Only the requester's answer counts.** The gate posts into a shared thread, so
anyone can type "y". The pending approval records who asked, and an affirmative
from anybody else is ignored — not treated as a decline, ignored, so a bystander
cannot approve *or* cancel somebody else's write.

**Timeout declines.** An unanswered gate that eventually proceeds is not a gate,
it is a delay. The fail-safe direction is do-not-run, the same as `input()`
returning anything other than "y".

**Blocking the worker is the correct behaviour, not a limitation.** While a gate
is pending the single worker is parked on purpose, and `channel.queue` rejects
new interactive work with a reason that says so. The alternative — running other
turns while a write waits for approval — would mean the state the human is
approving against can change between the question and the answer.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agentlib.session import current_constraints

#: Answers that approve. Everything else declines, including silence, "ok",
#: "sure" and "go ahead" — a gate should not be doing intent classification, and
#: an ambiguous approval is a decline.
AFFIRMATIVE: frozenset[str] = frozenset({"y", "yes", "approve", "approved"})

#: Answers that decline explicitly. Recognised only so the gate can stop waiting
#: early; anything unrecognised also declines, just at the timeout.
NEGATIVE: frozenset[str] = frozenset({"n", "no", "deny", "denied", "cancel"})

DEFAULT_TIMEOUT_SECONDS = 180.0


@dataclass
class PendingApproval:
    """One gated call, waiting on a human.

    `user_id` and `thread_key` together are the address the answer must come
    from. Both, not either: the same person in a different thread is answering a
    question they cannot see the context of.
    """

    tool: str
    args: dict[str, Any]
    user_id: str
    thread_key: str
    preview: str
    created_at: float = field(default_factory=time.time)

    def age(self) -> float:
        return time.time() - self.created_at


def preview_args(name: str, args: dict[str, Any]) -> str:
    """A human-readable summary of a gated call, with what constrains it.

    A whole file's contents in a chat message is unreadable, and an unreadable
    approval prompt gets approved reflexively — which is worse than no gate,
    because it launders the write through a human who did not actually look.

    The constraints matter for the same reason and are the harder half. Reducing
    the payload stops the human skimming; naming the decisions stops them
    approving a write that a recorded decision forbids, which is a thing that
    happened on a real run of this system. Nothing upstream of the gate is
    guaranteed to catch that case — the planner records constraint ids without
    judging them, and the executor's refusal is model judgement — so the gate is
    the last place it can be caught, and it can only be caught by whoever is
    reading the prompt. They need the facts the model had.

    Both gates render through this, so the CLI and the channel describe the same
    action the same way.
    """
    shown = dict(args)
    body = shown.get("new_content")
    if isinstance(body, str):
        shown["new_content"] = f"<{len(body)} chars, {body.count(chr(10)) + 1} lines>"
    line = f"{name}({shown})"

    constraints = current_constraints()
    if not constraints:
        return line
    return (line + "\n  Decisions on file for this change — read before approving:\n"
            + "\n".join(f"    - {c}" for c in constraints))


class ChannelGate:
    """An approval gate whose answer arrives as another channel message.

    Usage from the worker:

        gate = ChannelGate(send=channel.send)
        run_agent(..., approve=gate.callback_for(user_id, thread_key))

    and from the reader side, for every inbound message:

        if gate.submit_answer(user_id=..., thread_key=..., text=...):
            continue   # consumed as a gate answer, not a new request
    """

    def __init__(
        self,
        send: Callable[[str, str], None],
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
          send     `send(thread_key, text)` — how the gate asks its question.
                   Deliberately not a `Channel`: the gate needs to speak, not to
                   know what it is speaking through.
          timeout  seconds to wait before declining.
        """
        self._send = send
        self._timeout = timeout
        self._cond = threading.Condition()
        self._pending: Optional[PendingApproval] = None
        self._answer: Optional[bool] = None

    # --- state the queue consults --------------------------------------------

    @property
    def pending(self) -> Optional[PendingApproval]:
        """The gated call currently waiting, or None."""
        with self._cond:
            return self._pending

    def is_waiting_on(self, *, user_id: str, thread_key: str) -> bool:
        """True iff a gate is pending for exactly this person in this thread."""
        p = self.pending
        return bool(p and p.user_id == user_id and p.thread_key == thread_key)

    # --- the loop-facing half -------------------------------------------------

    def callback_for(
        self, user_id: str, thread_key: str
    ) -> Callable[[str, dict[str, Any]], bool]:
        """Build an `approve(name, args) -> bool` bound to one requester.

        The returned callable is what `run_agent` already expects, so nothing in
        the loop or in `guards.GATED` has to know the answer now arrives over a
        network.
        """

        def approve(name: str, args: dict[str, Any]) -> bool:
            return self._ask(name, args, user_id=user_id, thread_key=thread_key)

        return approve

    def _ask(
        self, name: str, args: dict[str, Any], *, user_id: str, thread_key: str
    ) -> bool:
        preview = preview_args(name, args)
        with self._cond:
            self._pending = PendingApproval(
                tool=name, args=dict(args), user_id=user_id,
                thread_key=thread_key, preview=preview,
            )
            self._answer = None

        self._send(
            thread_key,
            "Approval needed for an irreversible action:\n"
            f"    {preview}\n"
            f"Reply 'y' to approve, anything else declines. "
            f"No answer within {int(self._timeout)}s declines.",
        )

        deadline = time.time() + self._timeout
        with self._cond:
            while self._answer is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            answer = self._answer
            self._pending = None
            self._answer = None

        if answer is None:
            self._send(thread_key, f"No answer in time — declined: {name}.")
            return False
        self._send(
            thread_key,
            f"{'Approved' if answer else 'Declined'}: {name}.",
        )
        return bool(answer)

    # --- the channel-facing half ---------------------------------------------

    def submit_answer(self, *, user_id: str, thread_key: str, text: str) -> bool:
        """Offer an inbound message to the pending gate.

        Returns True iff the message was CONSUMED as a gate answer, in which
        case the caller must not also treat it as a new request.

        A message from anyone other than the requester is not consumed and is
        not an answer — it falls through and is handled as ordinary input. A
        bystander in a group thread can neither approve nor cancel a write they
        did not ask for.
        """
        with self._cond:
            p = self._pending
            if p is None:
                return False
            if p.user_id != user_id or p.thread_key != thread_key:
                return False

            word = (text or "").strip().lower()
            # Unrecognised text from the requester still decides: they answered,
            # just not affirmatively. Only AFFIRMATIVE approves (fail-safe).
            self._answer = word in AFFIRMATIVE
            self._cond.notify_all()
            return True

    def cancel(self) -> None:
        """Abandon a pending gate as declined. Used on shutdown."""
        with self._cond:
            if self._pending is not None:
                self._answer = False
                self._cond.notify_all()
