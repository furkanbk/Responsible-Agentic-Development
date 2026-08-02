"""agentlib.langchain_tools — the one place a tool function becomes a LangChain tool.

HW4, decision #50. Tool functions in `tools/*.py` do not change: same signature,
same docstring-as-description, same `Literal[...]`-derived enum (LangChain infers
the args schema from type hints the same way `agentlib.schemas.schema_for` reads
them), same ambient `session_scope`/`impact_scope` reads (agentlib.session).
Nothing here adds an identity or scope *parameter* — invariant #25 holds exactly
because the wrapped function's signature is untouched; LangChain only sees
whatever parameters the author already wrote.

Tool authors never call `StructuredTool`/`@tool` directly. They write the same
kind of function HW1-HW3 already used, and it is converted at registry-build
time by `build_langchain_tools`.
"""

from __future__ import annotations

from typing import Callable

from langchain_core.tools import StructuredTool


def to_langchain_tool(fn: Callable) -> StructuredTool:
    """Wrap one plain tool function as a LangChain `StructuredTool`.

    `name` <- fn.__name__, `description` <- fn.__doc__ (stripped, decides tool
    selection — same rule as `schema_for`, Part A A4: say when AND when not to
    call). Args schema is inferred from `fn`'s type hints, so `Literal[...]`
    params still derive an enum-constrained field.
    """
    return StructuredTool.from_function(
        func=fn,
        name=fn.__name__,
        description=(fn.__doc__ or "").strip(),
        infer_schema=True,
    )


def build_langchain_tools(fns: list[Callable]) -> list[StructuredTool]:
    """Convert a list of tool functions, preserving order (the order the model sees them)."""
    return [to_langchain_tool(fn) for fn in fns]
