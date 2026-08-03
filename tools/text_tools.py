"""tools.text_tools — HW4 second new tool (Phase 13a, T13a / decision #54).

Owner: Alejandro Ramírez Trueba. Not a graph tool — like
`tools/utility_tools.py` it is an ordinary read-only function, here to be the
*second* framework-integrated tool the HW4 requirement asks for ("at least two
tools, the agent decides when to call them", Part 2). It is registered through
the one conversion point (`agentlib.langchain_tools.to_langchain_tool`), the
same as every other tool — nothing here is hand-wrapped as a LangChain tool
(decision #50).

It pairs with `tools/apply_change.py` (mine, HW2): `apply_change` is how an
edit lands; `diff_texts` is how the agent shows exactly what an edit changes
instead of asserting "I updated the docstring" and hoping. A diff is computed,
not eyeballed — same reason `evaluate_expression` exists.

Read-only and reversible -> ungated (CLAUDE.md §5): comparing two strings can
damage nothing, so it earns no approval ceremony.
"""

from __future__ import annotations

import difflib
from typing import Literal

# Guard against an unbounded request: the model could paste two very large
# blobs and difflib is quadratic in the worst case. Over the cap we refuse with
# a structured error (its own branch, never a truncated diff dressed as a full
# one) rather than silently comparing prefixes.
_MAX_CHARS = 200_000


def diff_texts(
    before: str,
    after: str,
    mode: Literal["unified", "ndiff"] = "unified",
) -> dict:
    """Compute the line diff between two text blobs and report what changed.

    Use when you need to SHOW the exact difference between two versions of some
    text — before/after an edit, an expected vs. actual block, two variants a
    user pasted — rather than describing the change in prose and risking a
    mismatch. `mode="unified"` gives the compact `+`/`-` hunk view; `mode="ndiff"`
    gives the line-by-line view with intra-line change hints.

    When NOT to call: you already have the diff, or the inputs are not text you
    can meaningfully compare line-by-line (binary, or two unrelated blobs where
    a diff is noise). This tool compares; it does not apply anything — writing a
    change is `apply_change`, and that one is gated.

    Returns (contract): {"mode": str, "changed": bool, "diff": [<str>, ...]} on
    success — `changed` is False with an empty `diff` when the two are identical.
    On oversized input: {"error": "input_too_large", "details": [<str>]} (own
    branch — never a partial diff presented as complete).
    """
    if len(before) + len(after) > _MAX_CHARS:
        return {
            "error": "input_too_large",
            "details": [
                f"combined input is {len(before) + len(after)} chars; "
                f"the limit is {_MAX_CHARS}"
            ],
        }

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if mode == "ndiff":
        diff = [
            line for line in difflib.ndiff(before_lines, after_lines)
            # ndiff emits '? ' hint lines and '  ' context; keep only real
            # change markers so the result is the change, not the whole file.
            if line[:1] in ("+", "-")
        ]
    else:
        diff = list(
            difflib.unified_diff(before_lines, after_lines, lineterm="")
        )

    return {"mode": mode, "changed": before_lines != after_lines, "diff": diff}
