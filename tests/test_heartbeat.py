"""tests/test_heartbeat.py — the monitor's clock (HW3, T11.1/T11.4).

Owner: Dias Sarkytbaev. New file.

The heartbeat drives `monitor.judge` from outside on a threshold of unjudged
records. The judge is INJECTED here so the mechanics — the threshold, the
watermark, "problems are posted, a clean pass is silent and recorded" — are
tested deterministically without a live model. `conftest` points all stores at a
temp dir, so the runs log, the watermark and the silences table are all throwaway.
"""

from __future__ import annotations

import json

from agentlib.runlog import runs_file
from monitor.judge import Verdict
from overlay import db as overlay_db
from triggers import heartbeat


def write_run(run_id: str) -> None:
    """Append one minimal run-log record to the isolated runs.jsonl."""
    path = runs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"run_id": run_id, "agent": "executor", "assembled": {"instructions": ""},
           "steps": [], "stopped": "answered", "answer": "ok"}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec) + "\n")


def clean_judge(runs):
    return [Verdict(run_id=r["run_id"], prompt_adherence="strictly_adheres",
                    grounding="grounded") for r in runs]


def problem_judge(runs):
    # One serious in-context violation makes the verdict a problem.
    return [Verdict(run_id=runs[-1]["run_id"], prompt_adherence="serious_violation",
                    grounding="grounded",
                    violations=[{"rule": "R7", "severity": "serious",
                                 "expected": "x", "observed": "y"}])]


def silence_count(user_id=None) -> int:
    conn = overlay_db.connect()
    try:
        return overlay_db.count_silences(conn, user_id=user_id)
    finally:
        conn.close()


class TestThreshold:
    def test_below_threshold_does_nothing(self):
        for i in range(2):
            write_run(f"r{i}")
        posted = []
        out = heartbeat.run_once(threshold=5, judge=clean_judge, post=posted.append)
        assert out["acted"] is False and out["pending"] == 2
        assert posted == [] and silence_count() == 0

    def test_threshold_reached_acts(self):
        for i in range(5):
            write_run(f"r{i}")
        out = heartbeat.run_once(threshold=5, judge=clean_judge, post=lambda v: None)
        assert out["acted"] is True and out["judged"] == 5


class TestCleanPassIsSilentAndRecorded:
    def test_clean_pass_posts_nothing_and_writes_one_silence_row(self):
        for i in range(5):
            write_run(f"r{i}")
        posted = []
        out = heartbeat.run_once(threshold=5, judge=clean_judge, post=posted.append)
        assert out["problems"] == []          # nothing to say
        assert posted == []                    # ...and it said nothing
        assert out["silence_id"] is not None   # but recorded WHY it was silent
        assert silence_count() == 1


class TestProblemsArePosted:
    def test_problems_go_to_the_poster_and_record_no_silence(self):
        for i in range(5):
            write_run(f"r{i}")
        posted = []
        out = heartbeat.run_once(threshold=5, judge=problem_judge, post=posted.append)
        assert len(out["problems"]) == 1
        assert len(posted) == 1 and posted[0] == out["problems"]
        assert out["silence_id"] is None and silence_count() == 0


class TestWatermark:
    def test_a_second_pass_grades_only_new_records(self):
        for i in range(5):
            write_run(f"r{i}")
        seen: list[list[str]] = []

        def recording_judge(runs):
            seen.append([r["run_id"] for r in runs])
            return clean_judge(runs)

        heartbeat.run_once(threshold=5, judge=recording_judge, post=lambda v: None)
        assert seen[0] == [f"r{i}" for i in range(5)]        # graded all five
        assert heartbeat.read_watermark() == "r4"            # watermark advanced

        write_run("r5")
        heartbeat.run_once(threshold=1, judge=recording_judge, post=lambda v: None)
        assert seen[1] == ["r5"]                             # only the new one
        assert heartbeat.read_watermark() == "r5"

    def test_unjudged_returns_everything_when_watermark_is_unknown(self):
        runs = [{"run_id": "a"}, {"run_id": "b"}]
        assert heartbeat.unjudged(runs, None) == runs
        assert heartbeat.unjudged(runs, "gone") == runs
        assert heartbeat.unjudged(runs, "a") == [{"run_id": "b"}]
