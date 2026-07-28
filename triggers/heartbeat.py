"""triggers.heartbeat — the monitor's real clock.

Owner: Dias Sarkytbaev (HW3, T11.1).

HW2 decision #40 said the monitor runs "on its own clock", but it never had one:
`python -m monitor.judge` graded the whole log every time you ran it by hand. The
heartbeat is that clock — a background trigger that grades what is NEW and stays
quiet unless it finds something.

Two design choices, both about not being noise:

  * It fires on a THRESHOLD of unjudged records, not a timer. "Grade when N runs
    have piled up" is less arbitrary than "grade every six hours" — the work
    scales with what happened, not with the wall clock.
  * A clean pass SAYS NOTHING and records `heartbeat_clean` (T11.2 silence). A
    bot that posts "all clear" every interval gets muted, and a muted bot is
    useless on the one pass that finds a real problem. Silence is the default;
    a message is earned by `problems(verdicts)` being non-empty.

A persisted watermark means each pass grades only records newer than the last,
so the judge is never re-run (and re-billed) on runs already graded.

The heartbeat drives `monitor.judge` from OUTSIDE — judge.py stays read-only
(decision #40). This module reads runs, calls the judge, and posts problems; it
never touches how a verdict is formed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from agentlib.runlog import read_runs, runs_dir
from channel.silence import REASON_HEARTBEAT_CLEAN, TEAM
from monitor.judge import Verdict, judge_runs, problems, report
from overlay import db as overlay_db

# A judge takes the pending records and returns verdicts. Injected so a test can
# grade deterministically without a live model (the real default calls the model
# through monitor.judge).
Judge = Callable[[list[dict[str, Any]]], list[Verdict]]

# A poster takes the problem verdicts and delivers them (to the team thread, in
# service.py). Injected so a test — and an offline run — can capture instead of send.
Poster = Callable[[list[Verdict]], None]


def _watermark_path() -> Path:
    """Where the last-graded marker lives — beside runs.jsonl, same env knob."""
    override = os.environ.get("RADF_HEARTBEAT_WATERMARK")
    return Path(override) if override else runs_dir() / "heartbeat_watermark.json"


def read_watermark() -> Optional[str]:
    """The `run_id` of the newest record graded on the last pass, or None."""
    path = _watermark_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_run_id")
    except (json.JSONDecodeError, OSError):
        return None


def write_watermark(run_id: Optional[str]) -> None:
    """Persist the newest graded `run_id` so the next pass starts after it."""
    if not run_id:
        return
    path = _watermark_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_run_id": run_id}), encoding="utf-8")


def unjudged(runs: list[dict[str, Any]], watermark: Optional[str]) -> list[dict[str, Any]]:
    """The records after `watermark` (which is the last one already graded).

    If the watermark is not found in the log (rotated away, or a fresh start),
    everything is unjudged — the safe direction is to grade more, not skip.
    """
    if not watermark:
        return list(runs)
    for i, rec in enumerate(runs):
        if rec.get("run_id") == watermark:
            return runs[i + 1:]
    return list(runs)


def _default_post(verdicts: list[Verdict]) -> None:
    print(report(verdicts))


def run_once(
    *,
    threshold: int = 5,
    judge: Judge = judge_runs,
    post: Optional[Poster] = None,
    trigger: str = "heartbeat",
) -> dict[str, Any]:
    """One heartbeat pass: grade the backlog if it is big enough, act on problems.

    Returns a small summary dict:
      acted        did the threshold trip this pass?
      pending      how many unjudged records were waiting
      judged       how many were graded (0 unless acted)
      problems     the problem verdicts found (empty on a clean pass)
      silence_id   the recorded heartbeat_clean row when a clean pass acted
    """
    runs = read_runs()
    watermark = read_watermark()
    pending = unjudged(runs, watermark)

    # Threshold not reached — nothing to do, nothing to say. (A count is the
    # code's decision, not the model's — same spirit as the loop's stopping cap.)
    if len(pending) < threshold:
        return {"acted": False, "pending": len(pending), "judged": 0,
                "problems": [], "silence_id": None}

    verdicts = judge(pending)
    found = problems(verdicts)
    newest = runs[-1].get("run_id") if runs else None
    write_watermark(newest)                       # advance past everything graded

    summary: dict[str, Any] = {
        "acted": True, "pending": len(pending), "judged": len(pending),
        "problems": found, "silence_id": None,
    }

    if found:
        (post or _default_post)(found)            # a message is earned, not scheduled
        return summary

    # Clean pass: say nothing, but record WHY there was silence, so a quiet
    # heartbeat is distinguishable from a dead one (T11.2, decision #40).
    conn = overlay_db.connect()
    try:
        row = overlay_db.record_silence(
            conn, trigger=trigger, reason_code=REASON_HEARTBEAT_CLEAN,
            evidence=f"graded {len(pending)} run(s), no problems found",
            visibility=TEAM,
        )
    finally:
        conn.close()
    summary["silence_id"] = row["silence_id"]
    return summary


def run_forever(
    *,
    interval_s: float = 300.0,
    threshold: int = 5,
    judge: Judge = judge_runs,
    post: Optional[Poster] = None,
    ticks: Optional[int] = None,
) -> None:
    """The interval loop. `ticks` bounds it for tests; None runs until killed.

    The clock is the interval; the *work* is gated by the threshold inside it, so
    a burst of runs is graded promptly and a quiet stretch costs nothing.
    """
    count = 0
    while ticks is None or count < ticks:
        run_once(threshold=threshold, judge=judge, post=post)
        count += 1
        if ticks is not None and count >= ticks:
            break
        time.sleep(interval_s)


def _main() -> int:
    """Run one pass from the CLI — the manual tick of the background clock."""
    summary = run_once(threshold=int(os.environ.get("RADF_HEARTBEAT_THRESHOLD", "1")))
    if not summary["acted"]:
        print(f"heartbeat: {summary['pending']} pending, threshold not reached")
    elif summary["problems"]:
        print(f"\nheartbeat: {len(summary['problems'])} run(s) need attention")
    else:
        print(f"heartbeat: graded {summary['judged']}, all clean "
              f"(silence {summary['silence_id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
