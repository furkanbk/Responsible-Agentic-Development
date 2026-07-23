"""agentlib.schemas — derive a tool schema from a Python function.

This is the notebook's `schema_for(fn)` (Part A, A3), verbatim in behavior, with
one faithful extension: a `Literal[...]` annotation is derived into a JSON-Schema
`enum`. The stub signatures in tools/*.py already use `Literal[...]` for their
constrained params (kind, relation, status, scope, cascade), so deriving the enum
here is just reading the annotation the author already wrote — it is not authoring
new narrowing. Anything beyond what the signature states (required-ness beyond
defaults, numeric bounds, prose "when NOT to call") stays the tool author's job on
top of the derived schema (ARCHITECTURE.md §3, Part B, B1).

`schema_for` is pure and side-effect free: same function in, same schema out.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any, Callable

# Python type -> JSON Schema type. Unknown annotations fall back to "string",
# exactly as the notebook does.
_PYTYPE = {int: "integer", float: "number", str: "string", bool: "boolean"}


def _literal_values(annotation: Any):
    """Return the tuple of values if `annotation` is a typing.Literal, else None."""
    if typing.get_origin(annotation) is typing.Literal:
        return typing.get_args(annotation)
    return None


def _prop_for(annotation: Any) -> dict[str, Any]:
    """Build the JSON-Schema property fragment for one parameter annotation."""
    literal = _literal_values(annotation)
    if literal is not None:
        # Infer the JSON type from the literal members (they are homogeneous in
        # our tools: all str, or all int). Fall back to "string".
        member_type = _PYTYPE.get(type(literal[0]), "string") if literal else "string"
        return {"type": member_type, "enum": list(literal)}
    return {"type": _PYTYPE.get(annotation, "string")}


def schema_for(fn: Callable) -> dict[str, Any]:
    """Derive an OpenAI-tools schema from a function's signature + docstring.

    - name        <- fn.__name__
    - description  <- fn.__doc__ (stripped)  — this is what decides tool selection,
                    so it should say when AND when NOT to call (Part A, A4).
    - properties  <- each parameter's annotation (`Literal[...]` -> enum)
    - required    <- parameters with no default value

    Returns the dict the Responses API expects under `tools=[...]`. The author may
    further constrain the returned dict in place (numeric bounds, extra enums).
    """
    sig = inspect.signature(fn)
    # Resolve string annotations (PEP 563 / `from __future__ import annotations`)
    # back to real objects so `Literal[...]` derives an enum. Fall back to the raw
    # (possibly string) annotation if a hint can't be resolved.
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, p in sig.parameters.items():
        annotation = hints.get(name, p.annotation)
        props[name] = _prop_for(annotation)
        if p.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "function",
        "name": fn.__name__,
        "description": (fn.__doc__ or "").strip(),
        "parameters": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }
