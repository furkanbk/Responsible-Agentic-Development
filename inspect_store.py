"""inspect_store.py — look inside the stores.

Owner: Berat Furkan Kocak (HW2).

An entry point, like `main.py` and `orchestrator.py`: it calls no model, it just
reads. Four stores is past the point where `sqlite3` one-liners and `cat` are a
reasonable debugging story, and the interesting questions are cross-store ones —
"which agent read which version of the plan" spans `run_scratch` and
`scratch_reads`, and answering it by hand is how you stop bothering to check.

Usage:
    python inspect_store.py                 # everything, summarised
    python inspect_store.py decisions       # the authored overlay
    python inspect_store.py memory          # free-form memory
    python inspect_store.py runs            # run log, newest last
    python inspect_store.py trace <run_id>  # ONE run, step by step
    python inspect_store.py --user berat    # scope it to one user's view

`--user` is worth using: it applies the same visibility filter the agent gets,
so you can see exactly what one engineer's agent would and would not have been
shown. Without it you see everything, which is the admin view, not the agent's.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentlib.runlog import read_runs, runs_file
from overlay import db as overlay_db
from overlay import memory as mem

BAR = "─" * 78


def _h(title: str) -> None:
    print(f"\n{BAR}\n {title}\n{BAR}")


def _short(text: str, width: int = 68) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def show_decisions(user: str | None) -> None:
    _h(f"DECISIONS  ({overlay_db.db_path()})")
    if not overlay_db.db_path().exists():
        print("  (no overlay yet — run main.py or orchestrator.py once)")
        return
    conn = overlay_db.connect()
    try:
        rows = overlay_db.query_decisions(
            conn, user_id=user, statuses=("accepted", "proposed", "superseded")
        )
    finally:
        conn.close()

    if not rows:
        print(f"  (none visible to {user or 'anyone'})")
        return
    for r in rows:
        scope = r["symbol_uid"] or "· repo-wide ·"
        print(f"\n  {r['decision_id']}  [{r['status']}]  {r['visibility']}")
        print(f"    about    : {scope}")
        print(f"    author   : {r['author_id']}   at {r['ts']}")
        print(f"    decision : {_short(r['decision'])}")
        print(f"    rationale: {_short(r['rationale'])}")
    print(f"\n  {len(rows)} decision(s) visible to {user or 'anyone (admin view)'}")


def show_memory(user: str | None) -> None:
    _h(f"MEMORY  ({mem.memory_path()})")
    if not mem.memory_path().exists():
        print("  (nothing saved yet)")
        return
    rows = mem.all_memories(user)
    if not rows:
        print(f"  (none visible to {user or 'anyone'})")
        return
    for r in rows:
        star = " " if r["status"] == "accepted" else "?"
        print(f"\n {star} {r['memory_id']}  {r['kind']:5}  [{r['status']}]  {r['visibility']}")
        print(f"    text     : {_short(r['text'])}")
        print(f"    cue      : {r['cue'] or '(none)'}"
              f"{'   bound to ' + r['applies_to'] if r.get('applies_to') else ''}")
        print(f"    author   : {(r.get('source') or {}).get('author', '?')}"
              f"   used {r.get('use_count', 0)}x")
    print(f"\n  {len(rows)} memor(y/ies) visible to {user or 'anyone (admin view)'}")
    print("  '?' marks status=proposed — inferred, not yet shaping behaviour.")


def show_runs(limit: int) -> None:
    _h(f"RUNS  ({runs_file()})")
    records = read_runs(limit)
    if not records:
        print("  (no runs logged yet)")
        return
    for r in records:
        agents = " -> ".join(e["agent"] for e in r.get("envelopes") or []) or r["agent"]
        print(f"  {r['run_id']}  {r.get('user_id')}/{r.get('thread_id')}  "
              f"[{r['stopped']}]  {agents}")
        print(f"      {_short(r.get('request', ''), 64)}")
    print(f"\n  {len(records)} run(s). Drill in with:  "
          f"python inspect_store.py trace {records[-1]['run_id']}")


def show_trace(run_id: str) -> int:
    """One run, in order. This is the 'who talked to whom, when' view."""
    record = next((r for r in read_runs() if r["run_id"] == run_id), None)
    if record is None:
        print(f"no run {run_id!r} in {runs_file()}", file=sys.stderr)
        return 1

    _h(f"TRACE  {run_id}   {record.get('user_id')}/{record.get('thread_id')}")
    print(f"  request : {_short(record.get('request', ''), 66)}")
    print(f"  stopped : {record['stopped']}")

    assembled = record.get("assembled") or {}
    sources = assembled.get("sources") or {}
    print("\n  CONTEXT ASSEMBLY")
    print(f"    pushed  : {sources.get('pushed')}")
    for pulled in sources.get("pulled") or []:
        print(f"    pulled  : {pulled['source']} -> {pulled['count']} item(s)")
    print(f"    instructions: {len(assembled.get('instructions', ''))} chars")
    for block in assembled.get("data_blocks") or []:
        print(f"    quoted  : {_short(block.splitlines()[0], 62)}")

    if record.get("envelopes"):
        print("\n  AGENT HANDOFFS  (what each agent returned)")
        for e in record["envelopes"]:
            env = e["envelope"]
            print(f"    {e['agent']:10} -> status={env['status']:11} "
                  f"needs_approval={env['needs_approval']}")
            if env.get("notes"):
                print(f"               notes: {_short(env['notes'], 56)}")

    scratch = record.get("scratch") or {}
    if scratch.get("writes") or scratch.get("reads"):
        print("\n  SHARED MEMORY  (the channel with no call site)")
        for w in scratch.get("writes") or []:
            print(f"    seq {w['seq']:>3}  WRITE  {w['agent']:10} key={w['key']!r}"
                  f"  {_short(str(w['value']), 34)}")
        for r in scratch.get("reads") or []:
            saw = f"saw seq {r['saw_seq']}" if r["saw_seq"] else "MISS — found nothing"
            print(f"             READ   {r['agent']:10} key={r['key']!r}  -> {saw}")

    if record.get("steps"):
        print("\n  TOOL CALLS")
        for i, s in enumerate(record["steps"], 1):
            print(f"    {i}. [{s['branch']:12}] {s['tool']}({_short(str(s['args']), 40)})")
            print(f"           -> {_short(str(s['output']), 60)}")

    for change in record.get("applied_changes") or []:
        print(f"\n  FILE WRITTEN: {change.get('path')}  "
              f"{str(change.get('before_sha'))[:8]} -> {str(change.get('after_sha'))[:8]}")

    print(f"\n  answer  : {_short(record.get('answer') or '(none)', 66)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the RADF stores.")
    parser.add_argument("what", nargs="?", default="all",
                        choices=["all", "decisions", "memory", "runs", "trace"])
    parser.add_argument("run_id", nargs="?", default=None)
    parser.add_argument("--user", default=None,
                        help="apply this user's visibility filter (the agent's view)")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    if args.what == "trace":
        if not args.run_id:
            parser.error("trace needs a run_id — list them with: inspect_store.py runs")
        return show_trace(args.run_id)

    if args.what in ("all", "decisions"):
        show_decisions(args.user)
    if args.what in ("all", "memory"):
        show_memory(args.user)
    if args.what in ("all", "runs"):
        show_runs(args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
