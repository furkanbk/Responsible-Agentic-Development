"""tools.apply_change — the gated file write.

Owner: **Alejandro Ramírez Trueba** (HW2, T7.1).

*** THIS IS A STUB CONTRACT, NOT A GAP. ***

Written by Berat so the executor and orchestrator have something concrete to
build against, exactly as the Phase 0 stubs (T0.8) did for HW1. The signature,
the docstring, the error codes and the return shape are the contract. Fill in
the body; do not change the surface without agreement (CLAUDE.md §1).

The two confinement checks below are the reason this tool exists at all. HW1's
approval gate already handles "should this irreversible thing happen" — a human
says y or n. What it cannot do is bound WHAT the write may touch, because by
the time a human is reading a prompt they are approving a path the model chose.
Confinement is the part that has to be code.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Never writable, whatever the plan says. Secrets, git internals, and the two
# stores — an agent that can rewrite the overlay can rewrite its own memory and
# the decisions it is supposed to be constrained by.
DENYLIST = (".env", ".git", "store", "overlay", ".venv")


def sha256_of(text: str) -> str:
    """Content hash, used for the before/after record in the run log."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_change(
    path: str,
    new_content: str,
    intent: Literal["edit", "create"] = "edit",
) -> dict:
    """Write a file. IRREVERSIBLE — gated on explicit human approval.

    Use only for a change the plan already listed. Read the file first; this
    replaces its entire contents, so `new_content` must be the whole file, not
    a fragment or a diff.

    Constrained params:
      path         required; repo-relative path, e.g. "tools/decisions.py".
      new_content  required; the COMPLETE new contents of the file.
      intent       enum: "edit" (the file must already exist) or "create" (it
                   must not). Stating which prevents a typo'd path from
                   silently creating a new file next to the real one.

    When NOT to call: not to explore — use `read_source_file`. Not for a file
    outside the plan's impact set; if the change needs one, stop and say so, and
    let a human widen the plan. Not for anything under `store/` or `overlay/`:
    those are the agent's own memory and the decisions constraining it.

    Returns (contract), on success:
        {"path": <str>, "written": true, "intent": <str>,
         "before_sha": <str|null>, "after_sha": <str>, "bytes": <int>}

    Error branches (RETURNED, never raised):
        {"error": "path_outside_scope", "details": [...]}   outside repo root,
                                                            denylisted, or a
                                                            traversal/symlink
        {"error": "outside_impact_set", "impacted": [...]}  not in the plan
        {"error": "file_missing"}                           intent="edit", absent
        {"error": "file_exists"}                            intent="create", present
        {"error": "no_plan"}                                no impact set in force

    Implementation notes (T7.1a-c):
      * Resolve BEFORE comparing. `Path.resolve()` collapses `..` and follows
        symlinks; comparing the unresolved string lets `tools/../../etc/passwd`
        through, and a symlink in the repo point anywhere.
      * The impact set comes from `agentlib.session.current_impact_set()` —
        ambient, like the acting user. It must not be a parameter, for the same
        reason `author_id` is not: the model would fill it in, and would then be
        authorising its own writes with a list it just made up. An EMPTY impact
        set denies every write; it does not mean "unrestricted".
      * A refused write must leave the file BYTE-IDENTICAL. Check first, write
        second; never truncate-then-validate.
    """
    raise NotImplementedError(
        "T7.1 (Alejandro): implement path confinement, impact-set confinement, "
        "and the before/after hash record. See the docstring for the contract."
    )
