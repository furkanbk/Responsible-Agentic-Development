"""channel.telegram — the interactive surface.

Owner: Berat Furkan Kocak (HW3, T9.1).

Long-polls `getUpdates` and replies with `sendMessage`, over `urllib.request`.
No `requests`, no `python-telegram-bot`: CLAUDE.md §4 still binds and the Bot API
is JSON over HTTPS, which the standard library has covered since before any of
those existed. Adding a dependency here would buy retry logic we need to write
anyway (the backoff below is the interesting part) and an update-loop abstraction
that would hide the one thing worth seeing — where untrusted data enters.

The disposable identity
-----------------------
`TELEGRAM_BOT_TOKEN` is a **fresh bot's** token from @BotFather, not a personal
account. The token IS the account: anyone holding it is the bot. It is read from
`.env`, never committed, and never logged — `_url` builds the endpoint at call
time so the token does not sit in a formatted string anywhere it might be
printed with a traceback.

What is trusted here: nothing
-----------------------------
`message.from.id` is stamped by Telegram and is the only field with any standing,
and even it is treated as a lookup key rather than an identity
(`channel.identity`). Everything else on an update — text, display name, chat
title, reply quotes — is attacker-controlled in any group anyone can join. It
reaches the model as quoted data in `input[]` (decision #26), never as
instructions, and it is used here only to build an `InboundEvent`.

Failure is a branch, not a crash
--------------------------------
A channel that dies on one 502 is not a channel. Transport errors back off and
continue; the loop's only fatal condition is a missing token, which is a
configuration error rather than a runtime one. This mirrors §5's rule that a
tool failure gets its own branch rather than being dressed up as data.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

from .base import InboundEvent, OutboundReply

API_ROOT = "https://api.telegram.org"

#: Long-poll seconds. Telegram holds the connection open this long when idle, so
#: a higher number means fewer requests, not a slower response.
POLL_TIMEOUT = 25

#: Backoff bounds for transport failures.
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0

#: Telegram rejects messages over 4096 characters outright — an over-long reply
#: would fail as a whole rather than truncate, so it is clipped here.
MAX_MESSAGE_CHARS = 4000

_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
_OFFSET_ENV = "RADF_TELEGRAM_OFFSET_PATH"


def token() -> str:
    """The bot token, read at call time. Empty when unconfigured."""
    return os.environ.get(_TOKEN_ENV, "").strip()


def offset_path() -> Path:
    """Where the update offset is persisted, read at call time.

    Persisted because the offset is what acknowledges updates: without it a
    restart re-delivers everything Telegram still holds, and the agent answers
    a week-old question as though it were new.
    """
    override = os.environ.get(_OFFSET_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "store" / "telegram_offset.json"


class TelegramChannel:
    """A Telegram bot as a `channel.base.Channel`.

    Adapts the transport and nothing else: it runs no agent, opens no store, and
    does not decide whether to answer.
    """

    name = "telegram"
    source = "telegram"

    def __init__(
        self,
        bot_token: Optional[str] = None,
        *,
        poll_timeout: int = POLL_TIMEOUT,
        allowed_chats: Optional[set[str]] = None,
    ) -> None:
        """
        Args:
          bot_token      overrides `TELEGRAM_BOT_TOKEN` (tests, multiple bots).
          poll_timeout   seconds to hold each long-poll open.
          allowed_chats  if set, updates from any other chat id are ignored
                         before they ever become events. A bot's username is
                         guessable and anyone can start a chat with it; this is
                         the cheap way to keep the surface to known rooms.
        """
        self._token = bot_token if bot_token is not None else token()
        self._poll_timeout = poll_timeout
        self._allowed_chats = allowed_chats
        self._offset: Optional[int] = None
        self._backoff = BACKOFF_START

    # --- transport ------------------------------------------------------------

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self._token}/{method}"

    def _post(self, method: str, payload: dict[str, Any], timeout: float) -> Any:
        """One Bot API call. Raises on transport failure; callers branch on it."""
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._url(method),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            # An API-level refusal (bad token, bot kicked from the chat). Not a
            # transport error, so it is raised rather than retried blindly.
            raise RuntimeError(
                f"telegram {method} refused: {body.get('description', 'unknown')}"
            )
        return body.get("result")

    def configured(self) -> bool:
        """True iff a token is present. False means run without this channel."""
        return bool(self._token)

    def whoami(self) -> dict[str, Any]:
        """`getMe` — confirms the token works and says which bot it belongs to.

        Worth calling at startup: it turns "the bot is silent" into a
        configuration error at the moment the service starts, rather than a
        mystery an hour later.
        """
        return self._post("getMe", {}, timeout=15) or {}

    # --- offset persistence ---------------------------------------------------

    def _load_offset(self) -> Optional[int]:
        if self._offset is not None:
            return self._offset
        path = offset_path()
        if not path.exists():
            return None
        try:
            self._offset = int(json.loads(path.read_text(encoding="utf-8"))["offset"])
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            # A corrupt offset file is survivable: worst case some updates are
            # re-delivered. Refusing to start over it would be the wrong trade.
            self._offset = None
        return self._offset

    def _save_offset(self, offset: int) -> None:
        self._offset = offset
        path = offset_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"offset": offset}), encoding="utf-8")
        except OSError:
            # In-memory offset still advances, so the run stays correct even if
            # the file cannot be written. Only a restart would repeat work.
            pass

    # --- Channel protocol -----------------------------------------------------

    def poll(self) -> Iterator[InboundEvent]:
        """Yield inbound messages forever, backing off through transport errors."""
        if not self.configured():
            raise RuntimeError(
                f"{_TOKEN_ENV} is not set — create a fresh bot with @BotFather "
                "and put its token in .env (never a personal account's)."
            )
        while True:
            try:
                updates = self._get_updates()
                self._backoff = BACKOFF_START
            except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
                # The branch that keeps the channel alive. Sleeping here is
                # deliberate: the alternative is a hot loop against an endpoint
                # that is already unhappy.
                print(f"  [telegram] transport error ({exc}); retrying in "
                      f"{self._backoff:.0f}s")
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, BACKOFF_MAX)
                continue

            for update in updates:
                event = self.to_event(update)
                if event is not None:
                    yield event

    def _get_updates(self) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self._poll_timeout,
            "allowed_updates": ["message"],
        }
        offset = self._load_offset()
        if offset is not None:
            payload["offset"] = offset
        # HTTP timeout must outlast the long-poll or every poll looks like a
        # transport failure and the backoff engages on a healthy connection.
        updates = self._post("getUpdates", payload,
                             timeout=self._poll_timeout + 10) or []
        if updates:
            self._save_offset(max(u["update_id"] for u in updates) + 1)
        return updates

    def to_event(self, update: dict[str, Any]) -> Optional[InboundEvent]:
        """Turn one raw update into an `InboundEvent`, or None to ignore it.

        Public because it is the whole of the untrusted-input surface, and a
        test should be able to hand it a hostile payload without a network.
        """
        message = (update or {}).get("message") or {}
        text = message.get("text")
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")

        if not isinstance(text, str) or not text.strip() or chat_id is None:
            # Non-text messages (photos, joins, stickers) are not requests.
            return None

        thread_key = str(chat_id)
        if self._allowed_chats is not None and thread_key not in self._allowed_chats:
            return None

        return InboundEvent(
            source="telegram",
            thread_key=thread_key,
            text=text.strip(),
            # Only this field carries any weight, and only as a lookup key.
            external_user_id=str(sender.get("id")) if sender.get("id") else None,
            payload={
                "update_id": update.get("update_id"),
                "message_id": message.get("message_id"),
                "chat_type": chat.get("type"),
            },
            # Never set: a human's message is not coalescable (see channel.queue).
            dedupe_key=None,
        )

    def send(self, reply: OutboundReply) -> None:
        """Deliver a reply. A silent reply sends nothing at all."""
        if reply.silent or not (reply.text or "").strip():
            return
        self.send_text(reply.thread_key, reply.text)

    def send_text(self, thread_key: str, text: str) -> None:
        """Send one message, clipped to the API's limit. Never raises.

        A failed send must not kill the worker: the turn already happened, and
        losing the reply is better than losing the process that owes replies to
        everyone else in the queue.
        """
        body = text if len(text) <= MAX_MESSAGE_CHARS else (
            text[: MAX_MESSAGE_CHARS - 20] + "\n… (truncated)"
        )
        try:
            self._post(
                "sendMessage",
                {"chat_id": thread_key, "text": body,
                 "disable_web_page_preview": True},
                timeout=20,
            )
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
            print(f"  [telegram] send failed for {thread_key}: {exc}")
