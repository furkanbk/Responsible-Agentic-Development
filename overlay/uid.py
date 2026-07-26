"""overlay.uid — the join key between the overlay and whatever indexes structure.

Owner: Berat Furkan Kocak (HW2, T4.2).

`symbol_uid` was documentation-only through HW1: ARCHITECTURE.md §4/§6.1 and
CLAUDE.md §6 all name it, but no Python referenced it — `verify_graph_integrity`
joined on a bare `component` string against node ids u node paths (decision #13).

This module makes it real, and that is the whole point of doing it now rather
than later. ARCHITECTURE.md §6.1 promises that swapping the structural half for
GitNexus is "a uid remap, not a rewrite". That promise is only kept if the
overlay never stores a raw component string — every row keys on the output of
`resolve_uid`, so the migration is a change to THIS FILE and nothing else.

Format: ``"<Kind>:<path>"``.

    "agentlib.core"  ->  "Module:agentlib.core"
    "tools/loop.py"  ->  "Module:tools.loop"
    "README.md"      ->  "Doc:README.md"

GitNexus emits finer-grained uids of the same shape
(``"Function:src/embed.py:get_embeddings"``), so adopting the prefix now leaves
room to attach a decision to a function later without a schema change.
"""

from __future__ import annotations

from typing import Optional

# Kinds we mint today. GitNexus adds Function/Class/Process; the overlay stores
# whatever it is handed, so new kinds need no schema change here.
KIND_MODULE = "Module"
KIND_DOC = "Doc"

_DOC_SUFFIXES = (".md", ".rst", ".txt")


def resolve_uid(component: Optional[str]) -> Optional[str]:
    """Normalise a component reference into a stable `symbol_uid`.

    Accepts what the HW1 graph actually contains — a dotted module path
    ("agentlib.core"), a posix relpath ("tools/graph_query.py"), or a markdown
    relpath ("docs/ARCHITECTURE.md") — and returns one canonical form.

    `None` in, `None` out: a decision with no component is repo-wide, which is a
    legitimate row (`symbol_uid IS NULL`), not an error.

    Already-resolved uids pass through unchanged, so this is idempotent and safe
    to call on data that may have been through it before.

    When NOT to call: not for validating that a component exists. This is pure
    string normalisation — it never touches the graph. Whether the uid resolves
    to a real node is `verify_graph_integrity`'s question, and an unresolvable
    uid is ORPHANED, not deleted (CLAUDE.md §6).
    """
    if component is None:
        return None
    text = component.strip()
    if not text:
        return None

    # Idempotent: "Module:agentlib.core" in, same out. Guarded on a known kind
    # so a Windows-style "C:/x" or a stray colon is not mistaken for a prefix.
    if ":" in text:
        head = text.split(":", 1)[0]
        if head and head[0].isupper() and head.isalpha():
            return text

    posix = text.replace("\\", "/")

    if posix.endswith(_DOC_SUFFIXES):
        # Docs keep their path as-is; that is the id convention repo_scan uses
        # for markdown nodes (decision #17).
        return f"{KIND_DOC}:{posix}"

    if posix.endswith(".py"):
        # Path form -> the dotted module path repo_scan mints, so both spellings
        # of the same module collapse to one uid.
        dotted = posix.removesuffix(".py").replace("/", ".")
        dotted = dotted.removesuffix(".__init__")
        return f"{KIND_MODULE}:{dotted}"

    return f"{KIND_MODULE}:{text}"


def uid_matches_component(uid: Optional[str], component: Optional[str]) -> bool:
    """True iff `uid` is the resolved form of `component`.

    Used when joining overlay rows against structural nodes, which still speak
    in bare component strings. Keeps the comparison in one place so the join
    survives the GitNexus swap along with `resolve_uid`.
    """
    if uid is None or component is None:
        return uid is None and component is None
    return uid == resolve_uid(component)
