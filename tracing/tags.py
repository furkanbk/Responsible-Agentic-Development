"""tracing.tags — `request_origin` and `eval_case_id`, ambient (T15.5).

Owner: Berat Furkan Kocak.

**Ambient, never a parameter.** This is HW2 decision #25's rule in its second
application. If `request_origin` were an argument on `run_agent` — or worse, on a
tool — the MODEL would end up supplying it, from a context that contains other
users' untrusted text. A trace tag that the model can set is a trace tag an
attacker can set, and the first thing worth forging is the label that says which
traffic is worth auditing. Origin comes from the runtime that received the
request, exactly as identity does.

Origins are the taught three:

    api    a machine caller — the GitHub webhook (`triggers/webhook.py`)
    ui     a human at a keyboard — the CLI, the Telegram channel
    batch  something on a clock or a harness — the heartbeat, the orphan watch,
           and every eval run

`eval_case_id` is a **join key, not a filter**: it exists so a trace can be traced
back to the case that produced it. High-cardinality tags are a real warning for
`request_origin`-style filtering, and a bounded eval set is exactly the place
where a per-case tag is safe.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator, Optional

ORIGINS: tuple[str, ...] = ("api", "ui", "batch")

_ORIGIN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "radf_request_origin", default=None
)
_EVAL_CASE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "radf_eval_case_id", default=None
)


def current_origin() -> Optional[str]:
    """The origin in force, or None outside any entry point."""
    return _ORIGIN.get()


def current_eval_case_id() -> Optional[str]:
    """The eval case in force, or None for interactive traffic."""
    return _EVAL_CASE.get()


@contextmanager
def request_origin_scope(origin: str) -> Iterator[str]:
    """Run a block as traffic from `origin`. Restores the previous value on exit.

    An unknown origin is a programming error and says so — the tag's only job is
    separating eval runs from interactive ones, and a typo silently defeats it.
    """
    if origin not in ORIGINS:
        raise ValueError(f"request_origin must be one of {ORIGINS}, got {origin!r}")
    token = _ORIGIN.set(origin)
    try:
        yield origin
    finally:
        _ORIGIN.reset(token)


@contextmanager
def eval_case_scope(case_id: Optional[str]) -> Iterator[Optional[str]]:
    """Run a block attributed to one eval case. `None` clears the attribution."""
    token = _EVAL_CASE.set(case_id)
    try:
        yield case_id
    finally:
        _EVAL_CASE.reset(token)


def trace_tags(**extra: Any) -> dict[str, str]:
    """The ambient tags, plus anything the caller adds. Empty values are dropped.

    A tag whose value is None is left off entirely rather than written as the
    string "None" — searching for traces that have no eval case is `eval_case_id
    NOT IN (...)`, and a literal "None" row silently joins the eval set.
    """
    tags: dict[str, str] = {}
    origin = current_origin()
    if origin:
        tags["request_origin"] = origin
    case_id = current_eval_case_id()
    if case_id:
        tags["eval_case_id"] = str(case_id)
    for key, value in extra.items():
        if value is None or value == "":
            continue
        tags[key] = str(value)
    return tags
