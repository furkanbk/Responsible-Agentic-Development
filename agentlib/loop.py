"""agentlib.loop — run_agent(): the observe -> reason -> act -> verify loop.

This is the Part A `run_agent` (A5), grown into the Part B safe loop (B1-B4):
the same round trip, now with the guards from guards.py wired into their own
branches, a human approval gate on irreversible tools, an error branch for
tool-returned errors, and a trace.

Stopping conditions — all the CODE's decision, never the model's (CLAUDE.md §5):
  answered    the model made no tool call and returned text
  max_steps   the step ceiling was hit (the floor guard, Part A A5)
  stalled     a tool call repeated identically (guards.detect_stall)
  declined    a gated call was declined and the model kept re-issuing it
  truncated   the model's own output hit the token cap (not a finished answer)

Returns: {"answer", "steps", "trace", "stopped"} (ARCHITECTURE.md §3).

The loop never trusts tool output as instructions — a result is wrapped as data
(`{"result": ...}`) before it re-enters context (CLAUDE.md §5, Part B B3).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from .core import CHEAP, call
from .guards import (
    call_signature,
    check_output,
    detect_stall,
    is_error_result,
    requires_approval,
    validate_args,
)


def run_agent(
    user_msg: str,
    schemas: list[dict[str, Any]],
    registry: dict[str, Callable],
    approve: Optional[Callable[[str, dict], bool]] = None,
    model: str = CHEAP,
    max_steps: int = 8,
    verbose: bool = True,
) -> dict[str, Any]:
    """Drive the agent loop over `schemas`/`registry` until a stopping condition.

    Args:
      user_msg  the user's request (becomes the first input item).
      schemas   tool schemas (from schema_for), passed to the model each turn.
      registry  name -> callable, the actual tools the loop dispatches.
      approve   callback(name, args) -> bool for gated tools. If a gated tool is
                reached and `approve` is None or returns False, the call is
                declined and a `declined` result is returned to the model instead
                of executing (Part B, B4). Reversible tools ignore this.
      model     model id (defaults to CHEAP).
      max_steps step ceiling; hitting it stops with `stopped="max_steps"`.
      verbose   print each step / branch as it happens.

    Returns {"answer", "steps", "trace", "stopped"}. `trace` is a list of dicts,
    one per executed/declined tool call, each tagged with its branch
    ("ok" | "error" | "declined" | "invalid_args").
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
    schema_by_name = {s["name"]: s for s in schemas}
    trace: list[dict[str, Any]] = []
    signatures: list[str] = []
    declined_signatures: set[str] = set()
    step = 0

    while True:
        # --- observe / reason: ask the model ---------------------------------
        r = call(messages=messages, model=model, tools=schemas)

        # VERIFY the model's OWN output: truncated text is not a finished result.
        tag, _ = check_output(r)
        if tag == "ERROR_BRANCH":
            if verbose:
                print("  [TRUNCATED] model output hit the token cap — not an answer")
            return _stop("truncated", None, step, trace)

        # No tool call -> the model chose to answer. Done.
        if not r.tool_calls:
            return _stop("answered", r.text, step, trace)

        # --- act: append the model's tool-call items, then dispatch each -----
        messages += r.output_items
        for tc in r.tool_calls:
            name, args, call_id = tc["name"], tc["arguments"], tc["call_id"]
            sig = call_signature(name, args)
            signatures.append(sig)

            # Stall guard: an identical call we've already made this run.
            if detect_stall(signatures):
                # If the repeated call is one the user declined, report it as a
                # decline (the model is re-issuing a blocked action), else stall.
                reason = "declined" if sig in declined_signatures else "stalled"
                if verbose:
                    print(f"  [{reason.upper()}] repeated call {name}({args})")
                return _stop(reason, None, step, trace)

            schema = schema_by_name.get(name)

            # Branch A: unknown tool or invalid args — error branch, not execution.
            if schema is None:
                out, branch = {"error": "unknown_tool", "tool": name}, "invalid_args"
            elif (errs := validate_args(schema, args)):
                out = {"error": "invalid_args", "tool": name, "details": errs}
                branch = "invalid_args"
                if verbose:
                    print(f"  [INVALID ARGS] {name}({args}) -> {errs}")

            # Branch B: gated (irreversible) tool — require explicit approval.
            elif requires_approval(name):
                if verbose:
                    print(f"  [GATE] agent wants to call {name}({args}). Approve? [y/n]")
                if approve is not None and approve(name, args):
                    out = registry[name](**args)
                    branch = "error" if is_error_result(out) else "ok"
                    if verbose:
                        print(f"  [GATE] approved -> {out}")
                else:
                    out = {"declined_by_user": True, "tool": name}
                    branch = "declined"
                    declined_signatures.add(sig)
                    if verbose:
                        print("  [GATE] declined -> returning 'declined' result to the model")

            # Branch C: reversible tool — run it straight through.
            else:
                out = registry[name](**args)
                # Error branch (B2): an honest structured error, surfaced as such.
                branch = "error" if is_error_result(out) else "ok"
                if verbose:
                    marker = "[ERROR BRANCH] " if branch == "error" else ""
                    print(f"  step {step + 1}: {marker}{name}({args}) -> {out}")

            trace.append({"tool": name, "args": args, "output": out, "branch": branch})
            # Tool output re-enters context as DATA, never as instructions.
            messages.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"result": out}, default=str),
            })

        step += 1
        if step >= max_steps:
            return _stop("max_steps", None, step, trace)


def _stop(stopped: str, answer: Optional[str], steps: int,
          trace: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the loop's return dict for a given stopping condition."""
    return {"answer": answer, "steps": steps, "trace": trace, "stopped": stopped}
