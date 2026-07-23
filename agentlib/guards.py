"""agentlib.guards — the mechanical guardrails the loop routes failures through.

Every guard here follows one rule (Part B, B1): a failure goes to its OWN branch,
never back into the model's context dressed as valid data. These functions make
that branching *decidable* — the loop (loop.py) calls them and acts on the result.

Contents:
- validate_args(schema, args) -> list[str]   input-schema validation (B1 guard 1)
- check_output(result)        -> (tag, data)  truncation check (B1 guard 2)
- is_error_result(out)        -> bool         is a tool's output a structured error? (B2)
- detect_stall(signatures)    -> bool         repeated identical call detection
- call_signature(name, args)  -> str          stable key for stall detection
- GATED                       set[str]        tools that require human approval
- requires_approval(name)     -> bool         the gate policy

The `GATED` set is the approval policy. Irreversibility decides membership
(Part B, B4): `prune_graph_node` permanently removes a node, so it is gated;
every reversible tool (scan, query, append, verify) is NOT — no ceremony where
it isn't earned (CLAUDE.md §5).
"""

from __future__ import annotations

import json
from typing import Any

# --- The approval policy (which tools are gated) -----------------------------
#
# Only irreversible actions are gated. `prune_graph_node` is the sole destructive
# tool in HW1 (tools/graph_write.py, owned by Dias). If a reversibility-first
# variant lands (soft-delete + restore, Part B B5 / TODO T2.6), its soft-delete
# is removed from this set — that is the whole point of engineering the gate away.
GATED: set[str] = {"prune_graph_node"}


def requires_approval(name: str) -> bool:
    """True iff calling tool `name` must pause for explicit human approval."""
    return name in GATED


# --- Guard 1: input-schema validation ---------------------------------------

def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    """Validate model-supplied `args` against a tool `schema`; return error strings.

    Catches hallucinated/malformed arguments at the door, BEFORE the tool runs
    (Part B, B1 guard 1). An empty list means the args are acceptable. Checks:
    required-present, no-unknown-keys, enum membership, integer type.
    """
    spec = schema["parameters"]
    errors: list[str] = []
    for req in spec.get("required", []):
        if req not in args:
            errors.append(f"missing required '{req}'")
    for k, v in args.items():
        prop = spec["properties"].get(k)
        if prop is None:
            errors.append(f"unknown arg '{k}'")
            continue
        if "enum" in prop and v not in prop["enum"]:
            errors.append(f"'{k}'={v!r} not in {prop['enum']}")
        if prop.get("type") == "integer" and not isinstance(v, int):
            errors.append(f"'{k}' must be integer, got {type(v).__name__}")
    return errors


# --- Guard 2: output / truncation validation ---------------------------------

def check_output(result: Any) -> tuple[str, Any]:
    """Decide whether a model Result is usable. `it returned` != `it finished`.

    Returns ("ERROR_BRANCH", reason) if the output was truncated at the token cap
    — such text must NOT be fed back as data — else ("OK", result.text).
    Mirrors Part B, B1 guard 2, reading `Result.truncated` from core.
    """
    if getattr(result, "truncated", False):
        return ("ERROR_BRANCH", "truncated output is not a result — do not use as data")
    return ("OK", getattr(result, "text", None))


# --- Error-branch detection (B2) --------------------------------------------

def is_error_result(out: Any) -> bool:
    """True iff a tool returned a structured error (`{"error": ...}`).

    This is how the loop recognizes an honest error to branch on (e.g.
    verify_graph_integrity -> {"error": "graph_integrity_failed", ...}) versus a
    plausible-but-wrong value it can't detect (Part B, B2). A structured error is
    still returned to the model — but labeled as an error branch, never dressed as
    a successful result.
    """
    return isinstance(out, dict) and "error" in out


# --- Stall detection ---------------------------------------------------------

def call_signature(name: str, args: dict[str, Any]) -> str:
    """A stable, hashable key for one tool call — used to detect repeats."""
    return name + "|" + json.dumps(args, sort_keys=True, default=str)


def detect_stall(signatures: list[str]) -> bool:
    """True iff the most recent call repeats one already made this run.

    A repeated identical (name + args) call means the agent is spinning rather
    than progressing — a stopping condition the code owns, not the model
    (README "Safety properties"; CLAUDE.md §5). Expects the running list of
    signatures with the newest last.
    """
    if len(signatures) < 2:
        return False
    return signatures[-1] in signatures[:-1]
