"""agentlib.runlog — what happened, written down for a judge to read later.

Owner: Berat Furkan Kocak (HW2, T7.2).

The monitor runs on its own clock, after the fact, over these records. That
constrains what has to be in them: the judge cannot re-run anything, cannot ask
a follow-up, and cannot inspect live state. If a fact is not in the log, the
judge either hallucinates it or cannot grade the axis at all.

So a record carries what was ASSEMBLED as well as what was done. "The agent
ignored a rule" and "the rule was never in its context" look identical from the
outside and have completely different fixes — the first is a model failure, the
second is mine. Logging `instructions` and the pulled source ids is what keeps
those two apart.

One JSON object per line in `store/runs/runs.jsonl`. Append-only.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_RUNS_DIR = _REPO_ROOT / "store" / "runs"


def runs_dir() -> Path:
    """Where run logs live. `RADF_RUNS_DIR` is read at call time."""
    override = os.environ.get("RADF_RUNS_DIR")
    return Path(override) if override else _DEFAULT_RUNS_DIR


def runs_file() -> Path:
    return runs_dir() / "runs.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunLog:
    """One run, accumulated in memory and flushed once at the end."""

    agent: str
    user_id: Optional[str] = None
    thread_id: Optional[str] = None
    request: str = ""
    run_id: str = field(default_factory=lambda: f"r_{uuid.uuid4().hex[:12]}")
    started_at: str = field(default_factory=_now)

    instructions: str = ""
    data_blocks: list[str] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)

    steps: list[dict[str, Any]] = field(default_factory=list)
    applied_changes: list[dict[str, Any]] = field(default_factory=list)
    envelopes: list[dict[str, Any]] = field(default_factory=list)
    scratch: dict[str, Any] = field(default_factory=dict)

    stopped: str = ""
    answer: Optional[str] = None
    ended_at: Optional[str] = None

    # HW6: the MLflow trace for this run, if tracing was on. A FIELD, not a
    # `scratch` key — the orchestrator overwrites `scratch` wholesale with the
    # run's scratch table dump, which would silently drop the join. The trace is
    # the derived record; this file is the durable one, and the id is how a
    # scorer gets from one to the other (decision #89).
    trace_id: Optional[str] = None

    def record_envelope(self, agent: str, envelope: dict[str, Any]) -> None:
        self.envelopes.append({"agent": agent, "envelope": envelope, "ts": _now()})

    def record_change(self, entry: dict[str, Any]) -> None:
        self.applied_changes.append({**entry, "ts": _now()})

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "request": self.request,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "assembled": {
                # The full instructions, not a summary: "was the rule even
                # there?" has to be answerable from the log alone.
                "instructions": self.instructions,
                "data_blocks": self.data_blocks,
                "sources": self.sources,
            },
            "steps": self.steps,
            "envelopes": self.envelopes,
            "applied_changes": self.applied_changes,
            "scratch": self.scratch,
            "stopped": self.stopped,
            "answer": self.answer,
            "trace_id": self.trace_id,
        }

    def flush(self) -> Path:
        """Append this run to the log. Returns the file written."""
        self.ended_at = self.ended_at or _now()
        path = runs_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.to_dict(), default=str) + "\n")
        return path


def read_runs(limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Load run records, newest last. Malformed lines are skipped, not fatal —
    a half-written line must not stop the monitor from grading the rest."""
    path = runs_file()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records[-limit:] if limit else records
