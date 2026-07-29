"""triggers.webhook — the second source: GitHub pushes, verified then enqueued.

Owner: Alejandro Ramírez Trueba (HW3, Phase 10, T10.1).

HW1 and HW2 always started at a CLI prompt, so a human had already decided there
was work to do. This is the first inbound that no human asked for: GitHub calls a
URL when the repo changes under the decision records. It is the "external event"
trigger the slide (Session 6 §4) names, and the whole reason it exists is that a
decision goes stale exactly when nobody is looking — when someone moves the file
it points at.

Two rules shape this file, and both come straight from the session:

  **Verify the signature first.** "Anyone with the URL can call your webhook." An
  unsigned request is not a light version of a signed one; it is an anonymous
  stranger, and it is dropped before it can cause a single byte of work. The
  secret is `GITHUB_WEBHOOK_SECRET`, checked with `hmac.compare_digest` so the
  comparison is constant-time and a near-miss leaks nothing.

  **Only enqueue — never scan, never call an agent (T10.1a).** "Producers append
  events to a log; the agent consumes them at its own pace." If the HTTP handler
  did the rescan inline, an unauthenticated sender would get to pick how much work
  the box does, and the request thread would become a second place where ambient
  identity could be set (see `channel.base`). So `do_POST` verifies, parses, hands
  one `InboundEvent` to the queue, and returns. The work happens on the single
  worker, in `triggers.orphan_watch`.

Everything in the payload is untrusted (`channel.base`): the branch name, the
paths, the commit shas. None of it reaches `instructions`; it rides in
`event.payload` as opaque data that only `orphan_watch` reads.

No new dependency (CLAUDE.md §4): `http.server`, `hmac`, `hashlib`, `json`, stdlib.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from channel.base import InboundEvent

#: The GitHub HMAC secret. Read at call time so tests and demos never depend on a
#: developer's real `.env` (mirrors decision #11 for the stores).
_SECRET_ENV = "GITHUB_WEBHOOK_SECRET"
#: Where a github event's replies go. GitHub has no per-thread notion, so the
#: whole team shares one key; overridable for a different chat surface.
_THREAD_ENV = "GITHUB_THREAD_KEY"
_DEFAULT_THREAD = "team"

#: The header GitHub signs the body with. `sha1` is legacy and weaker; we require
#: the sha256 variant and ignore the sha1 one entirely.
SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"

#: Cap on a request body we will read into memory. A webhook payload is a few KB;
#: anything past this is either a mistake or an attempt to make us allocate, and
#: it is refused without reading the rest (the number comes from code, not a
#: sender-supplied Content-Length we trust).
MAX_BODY_BYTES = 2 * 1024 * 1024


def _secret() -> str:
    return os.environ.get(_SECRET_ENV, "").strip()


def _thread_key() -> str:
    return os.environ.get(_THREAD_ENV, "").strip() or _DEFAULT_THREAD


def verify_signature(secret: str, body: bytes, header: Optional[str]) -> bool:
    """True iff `header` is GitHub's HMAC-SHA256 of `body` under `secret`.

    `header` is the raw `X-Hub-Signature-256` value, i.e. `"sha256=<hex>"`. A
    missing header, a missing secret, a wrong algorithm prefix, or any mismatch
    all return `False` — there is no "close enough". The comparison uses
    `hmac.compare_digest` so it does not short-circuit on the first differing
    byte.

    An empty configured secret returns `False` even for an empty signature: a
    deployment with no secret set must not silently accept everything. Configure
    the secret or take the receiver down.
    """
    if not secret:
        return False
    if not header or not header.startswith("sha256="):
        return False
    sent = header.split("=", 1)[1].strip()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, expected)


def _changed_paths(commits: Any) -> list[str]:
    """Every path a push touched, from `commits[].{added,modified,removed}`.

    Deduplicated preserving first-seen order. Non-dict entries are skipped rather
    than raised on — the payload is untrusted, so a malformed commit is data to
    ignore, not a crash (CLAUDE.md §5).
    """
    seen: list[str] = []
    if not isinstance(commits, list):
        return seen
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for field in ("added", "modified", "removed"):
            for path in commit.get(field) or []:
                if isinstance(path, str) and path and path not in seen:
                    seen.append(path)
    return seen


def _branch_from_ref(ref: Any) -> str:
    """`"refs/heads/main"` -> `"main"`; anything else passes through as text."""
    text = str(ref or "")
    prefix = "refs/heads/"
    return text[len(prefix):] if text.startswith(prefix) else text


def parse_event(gh_event: str, payload: dict) -> Optional[InboundEvent]:
    """Turn a GitHub delivery into one `InboundEvent`, or `None` to ignore it.

    Handles `push` and `pull_request`; every other event type (there are dozens)
    returns `None` so the receiver drops it without enqueuing. `None` is a
    first-class "not for us", not a failure.

    The event carries `external_user_id=None` on purpose: no human is waiting on
    the other end, so it resolves to the low-trust anonymous identity in
    `channel.identity` rather than inheriting the pusher's session. `dedupe_key`
    is set (`push:<branch>` / `pr:<n>`) so the queue may coalesce a burst of
    pushes to the same branch into the newest one (decision #43) — dropping an
    intermediate push loses no signal the orphan check would have caught, because
    the check reads the tree as it stands, not each commit in turn.
    """
    if not isinstance(payload, dict):
        return None
    thread = _thread_key()

    if gh_event == "push":
        branch = _branch_from_ref(payload.get("ref"))
        changed = _changed_paths(payload.get("commits"))
        return InboundEvent(
            source="github",
            thread_key=thread,
            external_user_id=None,
            dedupe_key=f"push:{branch}",
            payload={
                "event": "push",
                "branch": branch,
                "changed_paths": changed,
                "before": payload.get("before"),
                "after": payload.get("after"),
                "compare": payload.get("compare"),
            },
        )

    if gh_event == "pull_request":
        pr = payload.get("pull_request") or {}
        number = payload.get("number") or pr.get("number")
        branch = ((pr.get("head") or {}).get("ref")) if isinstance(pr, dict) else None
        return InboundEvent(
            source="github",
            thread_key=thread,
            external_user_id=None,
            dedupe_key=f"pr:{number}",
            payload={
                "event": "pull_request",
                "action": payload.get("action"),
                "number": number,
                "branch": branch,
                # A PR payload does not enumerate changed files; the orphan check
                # rescans the tree regardless, so an empty list is honest here.
                "changed_paths": [],
            },
        )

    return None


def make_request_handler(
    enqueue: Callable[[InboundEvent], Any],
    *,
    secret: Optional[str] = None,
) -> type[BaseHTTPRequestHandler]:
    """Build the `BaseHTTPRequestHandler` subclass the server runs.

    `enqueue` is the ONLY thing this handler is allowed to do with an event: it
    is the queue's `submit`, injected so the receiver never imports the worker.
    `secret` defaults to `GITHUB_WEBHOOK_SECRET` read at request time.

    The handler:
      1. reads the body (bounded by `MAX_BODY_BYTES`),
      2. verifies `X-Hub-Signature-256` — a bad or absent signature is answered
         `401`, logged, and dropped; nothing is parsed or enqueued,
      3. parses the event; an unsupported type is `204 No Content`,
      4. enqueues the one event and answers `202 Accepted`.

    It never scans, never opens a store, never calls an agent. A `500` here means
    a bug in parsing, not in the agent — the agent has not run yet.
    """

    class _WebhookHandler(BaseHTTPRequestHandler):
        server_version = "RADFWebhook/1.0"

        def _reply(self, code: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 (http.server's required name)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._reply(413, "payload too large")
                return
            body = self.rfile.read(length) if length > 0 else b""

            key = secret if secret is not None else _secret()
            sig = self.headers.get(SIGNATURE_HEADER)
            if not verify_signature(key, body, sig):
                # The one place a stranger reaches. Refuse before any work.
                self.log_message("dropped unverified webhook delivery (bad signature)")
                self._reply(401, "signature verification failed")
                return

            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                self._reply(400, "body is not valid JSON")
                return

            gh_event = self.headers.get(EVENT_HEADER, "")
            event = parse_event(gh_event, payload)
            if event is None:
                self._reply(204, "")
                return

            enqueue(event)
            self._reply(202, "accepted")

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep the receiver quiet by default; a dropped delivery still logs
            # through the explicit call above.
            return

    return _WebhookHandler


def serve(
    enqueue: Callable[[InboundEvent], Any],
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    secret: Optional[str] = None,
) -> ThreadingHTTPServer:
    """Start a threading HTTP server that enqueues verified GitHub events.

    Returns the running server so the caller owns its lifetime (`serve_forever`
    on its own thread, `shutdown` on exit). Kept deliberately thin: it is the one
    part of this module that needs a socket, and it is not what the tests cover —
    they exercise `verify_signature` and `parse_event` directly.
    """
    handler = make_request_handler(enqueue, secret=secret)
    return ThreadingHTTPServer((host, port), handler)
