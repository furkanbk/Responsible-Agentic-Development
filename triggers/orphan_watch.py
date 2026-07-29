"""triggers.orphan_watch — what a GitHub push means for the decision records.

Owner: Alejandro Ramírez Trueba (HW3, Phase 10, T10.2/T10.3).

`webhook.py` verifies a push and drops it on the queue. This is what the single
worker does with it, and it is the payoff for the read path I have owned since
HW1: a push is the external clock that makes "a decision went stale" observable.

The loop, every time a github event surfaces:

  1. **Re-scan** the tree. Structure is derived (CLAUDE.md §6); the push means the
     derived half is now out of date, so `scan_repository_structure` regenerates
     it wholesale. This is the only side effect, and it touches only the derived
     graph — never the authored overlay.

  2. **Check integrity, read-only.** `verify_graph_integrity("all")` is Dias/
     Berat's tool; I consume its report, I do not reimplement its join. Its
     cross-store check is exactly the orphan signal: an overlay `symbol_uid` that
     no longer resolves to a node because the file moved.

  3. **Diff against a watermark.** The set of orphans is compared to the set the
     last pass recorded. Only *newly* orphaned decisions are surfaced — the
     count that decides whether to speak comes from code diffing two sets, never
     from the model (Session 6: "the number comes from code"). The current set is
     persisted as the new baseline.

  4. **Surface, or fall silent — and record either way.** A new orphan becomes one
     message naming its uid and the commit range: surfaced for review, never
     deleted (CLAUDE.md §6), because the component almost certainly just moved.
     A push that orphaned nothing is a deliberate non-answer, and silence is a
     branch that gets written down (T10.3): a `no_decisions_touched` row saying
     what was checked and why it kept quiet.

**The leak line.** An orphan message names only the `symbol_uid` — a code path,
which is structural — and the commit range. It never fetches or prints the
decision's text or its author. A decision may be private to one user; disclosing
its content, or even "user X has a private decision about this", to the team
thread would move a leak, not surface a signal. The uid is enough to review.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from channel.base import InboundEvent
from channel.silence import REASON_NO_DECISIONS_TOUCHED, TEAM
from overlay import db as overlay_db
from overlay.uid import resolve_uid
from tools.decisions import verify_graph_integrity
from tools.repo_scan import scan_repository_structure

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: What to scan when a push arrives. Overridable so a test points at a controlled
#: tree instead of the developer's checkout.
_ROOT_ENV = "RADF_SCAN_ROOT"
#: Where the last pass's orphan set is remembered. Defaults next to the graph so
#: `RADF_GRAPH_PATH` (which the test suite redirects) isolates it for free.
_WATERMARK_ENV = "RADF_ORPHAN_WATERMARK"
_GRAPH_ENV = "RADF_GRAPH_PATH"

#: Bounded recursion for the rescan. A repo this shape is a handful of levels
#: deep; the bound is the code's decision, not the pusher's.
SCAN_MAX_DEPTH = 32

#: The exact line `verify_graph_integrity` emits for an orphaned decision. Its
#: docstring freezes the "orphaned decision: symbol_uid '<uid>' ..." shape, so
#: parsing it is consuming a documented contract, not scraping prose.
_ORPHAN_LINE = re.compile(r"orphaned decision: symbol_uid '([^']*)'")


def _scan_root() -> str:
    return os.environ.get(_ROOT_ENV, "").strip() or str(_REPO_ROOT)


def _watermark_path() -> Path:
    override = os.environ.get(_WATERMARK_ENV)
    if override:
        return Path(override)
    graph = os.environ.get(_GRAPH_ENV)
    base = Path(graph).resolve().parent if graph else (_REPO_ROOT / "store")
    return base / "orphan_watermark.json"


def _load_watermark() -> set[str]:
    """The orphan uids the previous pass recorded. Missing/corrupt => empty set.

    A watermark that cannot be read is treated as "no baseline", so the next
    pass surfaces everything currently orphaned rather than staying silent on a
    bad file. Over-reporting once is recoverable; a silent guard is the failure
    mode the whole branch exists to avoid.
    """
    path = _watermark_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(data, dict):
        data = data.get("orphans", [])
    return {str(u) for u in data} if isinstance(data, list) else set()


def _save_watermark(orphans: set[str]) -> None:
    path = _watermark_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"orphans": sorted(orphans)}, indent=2), encoding="utf-8"
    )


def _orphan_uids(report: dict[str, Any]) -> set[str]:
    """The set of orphaned `symbol_uid`s named in a `verify_graph_integrity` report.

    A clean report (`{"ok": True, ...}`) yields the empty set. A failing report
    may list other problems too (duplicate nodes, orphan edges); only the
    orphaned-decision lines are extracted here — the rest are the graph's problem,
    not a decision's.
    """
    details = report.get("details") if isinstance(report, dict) else None
    if not isinstance(details, list):
        return set()
    found: set[str] = set()
    for line in details:
        match = _ORPHAN_LINE.search(str(line))
        if match:
            found.add(match.group(1))
    return found


def _decision_paths_touched(changed_paths: list[str], conn: Any) -> list[str]:
    """Which changed paths correspond to a component that carries a decision.

    Mechanical and code-owned (decision #27 spirit): each path is normalised
    through `resolve_uid` and intersected with the uids the overlay actually
    references. No model call decides relevance, so a wrong answer here traces to
    a path, never to model vibes.
    """
    known = set(overlay_db.all_decision_uids(conn))
    if not known:
        return []
    hits: list[str] = []
    for path in changed_paths or []:
        uid = resolve_uid(path)
        if uid in known and uid not in hits:
            hits.append(uid)
    return hits


def _commit_range(payload: dict[str, Any]) -> str:
    """A short, human-readable "where this came from" for an orphan message."""
    before, after = payload.get("before"), payload.get("after")
    if before and after:
        return f"{str(before)[:10]}..{str(after)[:10]}"
    branch = payload.get("branch")
    number = payload.get("number")
    if number:
        return f"PR #{number}"
    return f"branch {branch}" if branch else "(unknown range)"


def _orphan_message(uid: str, payload: dict[str, Any]) -> str:
    """One outbound line for one newly-orphaned decision (T10.2a).

    Names the uid and the commit range only. No decision text, no author — see
    the module docstring on the leak line.
    """
    return (
        f"⚠️ Orphaned decision: {uid} no longer resolves to a node after "
        f"{_commit_range(payload)}. The component likely moved — please re-point "
        f"the decision (surfaced for review, not deleted)."
    )


def handle_github_event(
    event: InboundEvent,
    *,
    send: Callable[[str, str], None],
    root: Optional[str] = None,
    verbose: bool = False,
) -> None:
    """Process one github event on the worker: rescan, diff orphans, surface/silence.

    Args:
      event    the `InboundEvent(source="github", ...)` the webhook enqueued.
      send     the channel's `send(thread_key, text)`. Called once per newly
               orphaned decision; not called at all on a silent outcome.
      root     directory to rescan; defaults to `RADF_SCAN_ROOT` or the repo root.
      verbose  print a one-line trace of the outcome.

    Returns nothing. Its effects are: the derived graph is regenerated, the
    orphan watermark is advanced, and either one-or-more messages are sent or a
    single silence row is recorded. It never raises on a bad payload — the field
    it reads (`changed_paths`) defaults to empty.
    """
    payload = event.payload or {}

    # 1. Re-derive structure. The push means the graph is stale.
    scan = scan_repository_structure(
        root=root or _scan_root(), max_depth=SCAN_MAX_DEPTH, kind="any"
    )
    if isinstance(scan, dict) and scan.get("error"):
        # A scan that refuses (e.g. a corrupt graph it will not overwrite) is a
        # branch, not a crash. Say nothing, record why, leave the watermark be.
        _record_silence(event, reason=REASON_NO_DECISIONS_TOUCHED,
                        evidence=f"rescan refused: {scan.get('error')}; nothing checked")
        if verbose:
            print(f"  [github] rescan refused ({scan.get('error')}) — silent")
        return

    # 2. Integrity check (read-only) -> current orphan set.
    report = verify_graph_integrity("all")
    current = _orphan_uids(report)

    # 3. Diff against the watermark. The count that decides comes from code.
    previous = _load_watermark()
    new_orphans = current - previous
    _save_watermark(current)

    # 4a. Newly orphaned decisions -> one message each. Surfaced, not deleted.
    if new_orphans:
        for uid in sorted(new_orphans):
            send(event.thread_key, _orphan_message(uid, payload))
        if verbose:
            print(f"  [github] surfaced {len(new_orphans)} newly-orphaned "
                  f"decision(s): {sorted(new_orphans)}")
        return

    # 4b. Nothing newly orphaned -> silence, recorded (T10.3). Evidence carries
    #     counts and uids only, never decision content.
    changed = payload.get("changed_paths") or []
    conn = overlay_db.connect()
    try:
        touched = _decision_paths_touched(changed, conn)
    finally:
        conn.close()
    evidence = (
        f"paths_changed={len(changed)} decision_paths_touched={len(touched)} "
        f"orphans_now={len(current)} orphans_new=0"
    )
    _record_silence(event, reason=REASON_NO_DECISIONS_TOUCHED, evidence=evidence)
    if verbose:
        print(f"  [github] no new orphans — silent ({evidence})")


def _record_silence(event: InboundEvent, *, reason: str, evidence: str) -> None:
    """Persist a deliberate non-answer against the team scope. See T10.3."""
    conn = overlay_db.connect()
    try:
        overlay_db.record_silence(
            conn, trigger=event.source, reason_code=reason,
            evidence=evidence, visibility=TEAM,
        )
    finally:
        conn.close()
