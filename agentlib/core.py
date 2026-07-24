"""agentlib.core — the single call path to the OpenCode Zen API.

This is the Session 3 `agentlib.tools.call(...)` scaffold, reorganized into
`agentlib.core` per the repo's ownership map (TODO.md) and component list
(ARCHITECTURE.md §3). The notebooks import `from agentlib.tools import call, ...`;
the surface here is identical — the only change is the module name the repo
standardizes on. If a session needs the notebook import path verbatim, add a
one-line `agentlib/tools.py` shim that re-exports from here rather than forking
this file.

Owns:
- `call(...) -> Result`   — OpenAI-compatible Responses call against Zen.
- `Result`                — carries text, tool_calls, output_items, status,
                            stop_reason, truncated, usage.
- `CHEAP` / `STRONG` / `MODELS` — pinned model ids + price table.
- `estimate_cost(usage)`  — cost accounting (cached input, reasoning output).
- `show(r, label)`        — one-line human-readable dump of a Result.

Contract note: `truncated=True` means the call hit the output-token cap.
Returned text is NOT a finished result — callers must branch on it (see
guards.check_output and Part B, B1 guard 2). The base URL and API key come
from the environment; the key is never hard-coded (CLAUDE.md, TODO T0.1).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - openai is a declared HW1 dependency
    OpenAI = None  # type: ignore

# --- Configuration -----------------------------------------------------------

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/zen/v1")
_API_KEY_ENV = "OPENCODE_API_KEY"

# --- Model ids & prices ------------------------------------------------------
#
# Ids confirmed against the Zen `/models` listing; prices from the Zen pricing
# page. Both are env-overridable so a swap needs no code change.
#
# Prices are USD per 1,000,000 tokens.
CHEAP = os.environ.get("OPENCODE_CHEAP_MODEL", "gpt-5.4-nano")
STRONG = os.environ.get("OPENCODE_STRONG_MODEL", "gpt-5.5")

# Keyed by literal model id, NOT by the CHEAP/STRONG variables: estimate_cost()
# looks up whatever id the caller actually passed, which may be neither.
MODELS: dict[str, dict[str, float]] = {
    # id: {input, cached_input, output}  — USD per 1M tokens.
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    # gpt-5.5 is priced for the <=272K-token context tier; longer contexts are
    # billed higher and are NOT modelled here.
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
}


# --- Result ------------------------------------------------------------------

@dataclass
class Result:
    """Normalized view of one Responses API reply.

    Fields (TODO T0.2 — all first-class):
      text          the assembled output text ("" if the model only called tools)
      tool_calls    list of {"name", "arguments" (parsed dict), "call_id"}
      output_items  the raw output items, ready to append to the next `input`
      status        provider status string (e.g. "completed", "incomplete")
      stop_reason   why generation stopped (e.g. "max_output_tokens", None)
      truncated     True iff the output-token cap was hit — text is NOT finished
      usage         {"input_tokens", "cached_input_tokens", "output_tokens",
                     "reasoning_tokens"} (best-effort; missing keys -> 0)
      raw           the untouched SDK response object (escape hatch)
    """

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    output_items: list[dict[str, Any]] = field(default_factory=list)
    status: Optional[str] = None
    stop_reason: Optional[str] = None
    truncated: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


# --- Client ------------------------------------------------------------------

_client: Optional["OpenAI"] = None


def _get_client() -> "OpenAI":
    """Lazily build one OpenAI client pointed at the Zen base URL.

    Reads the key from the environment (`OPENCODE_API_KEY`). Never hard-coded.
    """
    global _client
    if _client is not None:
        return _client
    if OpenAI is None:
        raise RuntimeError(
            "The 'openai' package is not installed. `pip install -r requirements.txt`."
        )
    api_key = os.environ.get(_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"{_API_KEY_ENV} is not set. Copy .env.example to .env and fill it in "
            "(the key is never hard-coded; see CLAUDE.md)."
        )
    _client = OpenAI(api_key=api_key, base_url=BASE_URL)
    return _client


# --- The call ----------------------------------------------------------------

def call(
    prompt: Optional[str] = None,
    *,
    messages: Optional[list[dict[str, Any]]] = None,
    system: Optional[str] = None,
    model: str = CHEAP,
    tools: Optional[list[dict[str, Any]]] = None,
    max_output_tokens: Optional[int] = None,
    **kwargs: Any,
) -> Result:
    """One round trip to the Zen Responses API, normalized into a `Result`.

    Accepts EITHER a bare `prompt` string OR a `messages` list (Responses
    `input` items) — exactly as the Session 3 notebooks call it:
        call("Reply with one word: ok", model=CHEAP, max_output_tokens=16)
        call(messages=convo, model=CHEAP, tools=SCHEMAS)

    `system` maps to Responses `instructions`. `tools` is a list of schemas from
    `schema_for(...)`. Pass exactly one of `prompt` / `messages`.

    Returns a `Result`. Note `Result.truncated`: returned text after hitting the
    token cap is not a finished answer — the caller must route it to an error
    branch, not feed it back as data.
    """
    if (prompt is None) == (messages is None):
        raise ValueError("call() needs exactly one of `prompt` or `messages`.")

    input_items = messages if messages is not None else [
        {"role": "user", "content": prompt}
    ]

    params: dict[str, Any] = {"model": model, "input": input_items}
    if system is not None:
        params["instructions"] = system
    if tools:
        params["tools"] = tools
    if max_output_tokens is not None:
        params["max_output_tokens"] = max_output_tokens
    params.update(kwargs)

    resp = _get_client().responses.create(**params)
    return _to_result(resp)


# Output items must be REPLAY-SAFE: `output_items` is fed straight back as the
# next turn's input (loop.py), so it may only contain items the Responses input
# schema accepts. Two things a raw model_dump() carries that the gpt-5.5 upstream
# (behind Zen) rejects with a 400 on replay — verified empirically:
#   - `reasoning` items: not accepted as replayed input here (no encrypted content
#     round-trips through the proxy), so we drop them. The nano upstream tolerates
#     them, which is why the bug only showed on the STRONG model.
#   - the server-assigned `id` on a `function_call` item: replaying an item with its
#     original server id 400s. `call_id` (the tool-call correlator) is kept; only
#     the decorative server `id` is stripped.
# See ARCHITECTURE.md decision record. `Result.raw` still holds the untouched resp.
_REPLAY_DROP_TYPES = {"reasoning"}
_SERVER_ONLY_FIELDS = {"id"}


def _replay_safe(item_dict: dict[str, Any]) -> dict[str, Any] | None:
    """Return a replay-safe copy of one output item, or None to drop it entirely."""
    if item_dict.get("type") in _REPLAY_DROP_TYPES:
        return None
    return {k: v for k, v in item_dict.items() if k not in _SERVER_ONLY_FIELDS}


def _to_result(resp: Any) -> Result:
    """Parse a Responses SDK object into a `Result` (defensive against shape drift)."""
    output_items: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []

    for item in getattr(resp, "output", None) or []:
        item_dict = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        safe = _replay_safe(item_dict)
        if safe is not None:
            output_items.append(safe)
        if item_dict.get("type") == "function_call":
            args = item_dict.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    # Malformed tool args are the loop's problem (validate_args),
                    # not ours to silently drop — hand them through as a raw dict.
                    args = {"__raw_arguments__": args}
            tool_calls.append({
                "name": item_dict.get("name"),
                "arguments": args or {},
                "call_id": item_dict.get("call_id"),
            })

    text = getattr(resp, "output_text", None) or ""

    status = getattr(resp, "status", None)
    incomplete = getattr(resp, "incomplete_details", None)
    stop_reason = None
    if incomplete is not None:
        stop_reason = getattr(incomplete, "reason", None) or (
            incomplete.get("reason") if isinstance(incomplete, dict) else None
        )
    truncated = status == "incomplete" and stop_reason == "max_output_tokens"

    return Result(
        text=text,
        tool_calls=tool_calls,
        output_items=output_items,
        status=status,
        stop_reason=stop_reason,
        truncated=truncated,
        usage=_extract_usage(getattr(resp, "usage", None)),
        raw=resp,
    )


def _extract_usage(usage: Any) -> dict[str, int]:
    """Pull token counts out of a Responses usage object into a flat dict."""
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return {}
    in_details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    return {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "cached_input_tokens": in_details.get("cached_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "reasoning_tokens": out_details.get("reasoning_tokens", 0) or 0,
    }


# --- Cost accounting ---------------------------------------------------------

def estimate_cost(usage: dict[str, int], model: str = CHEAP) -> float:
    """Estimate USD cost of a call from its usage dict.

    Accounting rules (ARCHITECTURE.md §3):
      - `input_tokens` ALREADY includes cached tokens. The cached subset is
        billed at the cheaper cached rate; the rest at the input rate.
      - `output_tokens` ALREADY includes reasoning tokens, billed at the normal
        output rate — no separate reasoning line.

    Returns 0.0 for an unknown model or empty usage (only the ids in MODELS are
    priced). Cost is derived, never authored — safe to recompute anytime.
    """
    price = MODELS.get(model)
    if not price or not usage:
        return 0.0
    input_tokens = usage.get("input_tokens", 0)
    cached = min(usage.get("cached_input_tokens", 0), input_tokens)
    fresh_input = input_tokens - cached
    output_tokens = usage.get("output_tokens", 0)
    cost = (
        fresh_input * price["input"]
        + cached * price["cached_input"]
        + output_tokens * price["output"]
    ) / 1_000_000
    return cost


# --- Display -----------------------------------------------------------------

def show(r: Result, label: str = "") -> Result:
    """Print a one-line human-readable summary of a Result; return it unchanged.

    Convenience for notebooks/CLI — not part of the loop's decision path.
    """
    tag = f"[{label}] " if label else ""
    calls = ", ".join(tc["name"] for tc in r.tool_calls) or "-"
    flags = "TRUNCATED " if r.truncated else ""
    print(
        f"{tag}status={r.status} stop={r.stop_reason} {flags}"
        f"tool_calls=[{calls}] text={r.text[:80]!r}"
    )
    return r
