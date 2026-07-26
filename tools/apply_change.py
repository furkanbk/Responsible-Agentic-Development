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

from agentlib.session import current_impact_set
from overlay.uid import resolve_uid

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Never writable, whatever the plan says. Secrets, git internals, and the two
# stores — an agent that can rewrite the overlay can rewrite its own memory and
# the decisions it is supposed to be constrained by.
DENYLIST = (".env", ".git", "store", "overlay", ".venv")


def sha256_of(text: str) -> str:
    """Content hash, used for the before/after record in the run log."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _confine(path: str) -> tuple[Path | None, dict | None]:
    """Resolve `path` inside the repo, or return a structured refusal.

    Same shape and rules as `tools.read_source._confine` (they share `DENYLIST`
    so the two cannot drift): resolve BEFORE comparing so `..` and symlinks are
    collapsed, then check the target is under the repo root and its top segment
    is not denylisted. This is T7.1a, and it lives in the tool — never in the
    prompt — because by the time a human is reading the gate prompt they are
    approving a path the model already chose.
    """
    try:
        target = (_REPO_ROOT / path).resolve()
    except (OSError, ValueError) as exc:
        return None, {"error": "path_outside_scope", "details": [str(exc)]}

    if target != _REPO_ROOT and _REPO_ROOT not in target.parents:
        return None, {"error": "path_outside_scope",
                      "details": [f"{path!r} resolves outside the repository"]}

    try:
        rel_parts = target.relative_to(_REPO_ROOT).parts
    except ValueError:
        return None, {"error": "path_outside_scope",
                      "details": [f"{path!r} resolves outside the repository"]}

    if rel_parts and rel_parts[0] in DENYLIST:
        return None, {"error": "path_outside_scope",
                      "details": [f"{rel_parts[0]!r} is off limits to tools — "
                                  "secrets, git internals, and the agent's own "
                                  "stores are never writable"]}
    return target, None


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
    if not isinstance(path, str) or not path.strip():
        return {"error": "invalid_args", "details": ["path must be a non-empty string"]}
    if not isinstance(new_content, str):
        return {"error": "invalid_args", "details": ["new_content must be a string"]}
    if intent not in ("edit", "create"):
        return {"error": "invalid_args",
                "details": [f"intent must be 'edit' or 'create', got {intent!r}"]}

    clean = path.strip()

    # T7.1a — path confinement. Refuse before looking at the plan or the disk:
    # a path outside the repo or on the denylist is refused whatever the plan says.
    target, refusal = _confine(clean)
    if refusal:
        return refusal

    # T7.1b — impact-set confinement. The set is ambient (the executor puts it
    # there with `impact_scope`); an EMPTY set denies every write. This is what
    # makes the planner's output load-bearing rather than advisory.
    impact = current_impact_set()
    if not impact:
        return {"error": "no_plan",
                "details": ["no impact set in force — a plan must authorise the write"]}
    uid = resolve_uid(clean)
    if uid not in impact:
        return {"error": "outside_impact_set", "impacted": list(impact)}

    exists = target.is_file()
    if intent == "edit" and not exists:
        return {"error": "file_missing",
                "details": [f"{clean!r} does not exist; use intent='create' to make it"]}
    if intent == "create" and exists:
        return {"error": "file_exists",
                "details": [f"{clean!r} already exists; use intent='edit' to replace it"]}

    # T7.1c — before-hash. Read the current bytes (only when editing) BEFORE the
    # write, so the run log records exactly what was replaced and a bad run is
    # revertible via git. Every branch above returned already, so reaching here
    # means the write is authorised and nothing has been touched yet.
    before_sha: Optional[str] = None
    if exists:
        try:
            before_sha = sha256_of(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            return {"error": "file_missing",
                    "details": [f"cannot read {clean!r} before writing: {exc}"]}

    try:
        if intent == "create":
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return {"error": "path_outside_scope",
                "details": [f"cannot write {clean!r}: {exc}"]}

    return {
        "path": clean,
        "written": True,
        "intent": intent,
        "before_sha": before_sha,
        "after_sha": sha256_of(new_content),
        "bytes": len(new_content.encode("utf-8")),
    }
