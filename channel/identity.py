"""channel.identity — who an inbound event is from, decided by the runtime.

Owner: Berat Furkan Kocak (HW3, T9.2).

HW2 established that identity is ambient and never a tool argument (decision
#25), and then set it from a `--user` flag. A flag is an honest placeholder for
authentication when the only caller is the person who typed it. A Telegram
group is not that: the id on an update is asserted by the platform, and the
display name next to it is asserted by the user, who may have set it to
"berat (admin)" ten seconds ago.

So exactly one thing here is trusted — the numeric `external_user_id` the
platform stamps on the update — and it is trusted only as a LOOKUP KEY. It is
never used as a RADF user id directly. An id nobody has mapped resolves to the
anonymous identity, not to itself; otherwise the first stranger to message the
bot picks their own scope, and `user:<whatever they chose>` is one collision
away from somebody's private rows.

The disposable identity, second layer
-------------------------------------
The assignment's "fresh bot account, never your primary account" is half a token
question. The other half is in the store: `BOT_AUTHOR_ID` is what the bot writes
as, so anything it authors is attributable to the bot and not to whoever asked
for it. Paired with an empty default impact set — which decision #25 already
defines as deny-every-write — read-only is the bot's posture by construction
rather than by prompt.

The anonymous identity
----------------------
An unmapped sender gets `SessionKey(user_id="")`. Empty is load-bearing in two
independent places, which is the point:

  * `visible_to("")` is falsy-checked and yields team rows only — a stranger
    reads what the team has published and nothing else.
  * `append_decision_record` refuses with `no_session` on a falsy author, so a
    stranger cannot write an attributable record.

Both of those are pre-existing HW2 behaviour, not new code. But they are
coincidences of a falsy check, and a coincidence is a bad thing to hang a trust
boundary on, so `Identity.can_write` states it directly and `service.py` hands
the anonymous path a read-only registry. Two mechanisms, deliberately.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from agentlib.session import SessionKey

from .base import InboundEvent

#: What the bot writes as. Never a human's id, whoever asked for the write.
BOT_AUTHOR_ID = "bot:radf"

#: The unmapped sender. Empty on purpose — see the module docstring.
ANONYMOUS_USER_ID = ""

_USERS_ENV = "RADF_CHANNEL_USERS"
_ADMINS_ENV = "RADF_CHANNEL_ADMINS"


@dataclass(frozen=True)
class Identity:
    """The resolved acting identity for one inbound event.

    Attributes:
      session   what `session_scope` will be entered with.
      known     True iff the sender was in the allowlist. False means anonymous.
      is_admin  True iff the resolved user is on the admin allowlist. This is
                necessary for the privileged path and NOT sufficient — T11.3
                also requires an explicit in-channel confirmation, because an
                allowlist alone makes every message from an admin a privileged
                one, including the ones they did not mean that way.
      external_id  the platform's id, kept for the audit trail only.
      source    which channel asserted it.
    """

    session: SessionKey
    known: bool
    is_admin: bool = False
    external_id: Optional[str] = None
    source: str = ""

    @property
    def can_write(self) -> bool:
        """True iff this identity may reach tools that record anything.

        An anonymous sender may read the team's published knowledge and may not
        add to it. Stated here rather than inferred from an empty string.
        """
        return self.known

    def __str__(self) -> str:
        who = self.session.user_id or "anonymous"
        return f"{who}@{self.source or '?'}" + (" [admin]" if self.is_admin else "")


def _parse_pairs(raw: str) -> dict[str, str]:
    """Parse `source:external_id=user_id,...` into a lookup map.

    Malformed entries are skipped rather than raising: a typo in one line of an
    allowlist must not take the whole channel down, and the failure mode of a
    skipped entry is that someone resolves anonymous — which is the safe side.
    """
    mapping: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            mapping[key] = value
    return mapping


def allowlist() -> dict[str, str]:
    """The `source:external_id` -> RADF user id map, read at CALL time.

    Call-time reads, same rule as `RADF_GRAPH_PATH` (decision #11): a test or a
    demo can point at its own allowlist without re-importing, and an operator
    can add a teammate without a restart being a correctness question.
    """
    return _parse_pairs(os.environ.get(_USERS_ENV, ""))


def admins() -> frozenset[str]:
    """RADF user ids permitted to reach the admin subagent (T11.3)."""
    raw = os.environ.get(_ADMINS_ENV, "")
    return frozenset(u.strip() for u in raw.split(",") if u.strip())


def _thread_id(event: InboundEvent) -> str:
    """A stable, bounded thread id from an untrusted routing key.

    The key comes off the wire, so it is clipped and stripped of anything that
    would make it awkward as a store value. It is an opaque routing token; no
    meaning is read out of it.
    """
    raw = "".join(ch for ch in (event.thread_key or "") if ch.isalnum() or ch in "-_:.")
    return raw[:64] or "default"


def resolve(event: InboundEvent) -> Identity:
    """Map an inbound event to the identity the run will act as.

    Machine-sourced events (github, heartbeat) have no `external_user_id` and
    resolve to anonymous by the same path a stranger does. That is intentional:
    nobody asked for them, so they inherit nobody's scope, and a trigger firing
    at 3am cannot act with the permissions of whoever last used the bot.
    """
    external = event.external_user_id
    thread = _thread_id(event)

    if not external:
        return Identity(
            session=SessionKey(user_id=ANONYMOUS_USER_ID, thread_id=thread),
            known=False, external_id=None, source=event.source,
        )

    key = f"{event.source}:{external}"
    user_id = allowlist().get(key)
    if not user_id:
        # Unmapped. The platform id is NOT reused as a RADF user id — see the
        # module docstring.
        return Identity(
            session=SessionKey(user_id=ANONYMOUS_USER_ID, thread_id=thread),
            known=False, external_id=str(external), source=event.source,
        )

    return Identity(
        session=SessionKey(user_id=user_id, thread_id=thread),
        known=True,
        is_admin=user_id in admins(),
        external_id=str(external),
        source=event.source,
    )
