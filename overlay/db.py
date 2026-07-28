"""overlay.db — SQLite for the authored layer.

Owner: Berat Furkan Kocak (HW2, T4.1 / T4.3).

Why relational, when the free-form memory next door is JSON: **the query is the
product.** The question this project exists to answer — "which accepted team
decisions touch any module affected by this change?" — is a join against an
impact set. In JSON that is a full scan and a hand-rolled filter on every run.
Schema stability is a feature for the decision layer and a cost for learned
facts, so the two layers split on that, not on "might the fields change".

Tables:
    decisions      the authored domain model: what was decided, by whom, about
                   which symbol, visible to whom, at what point in its lifecycle
    runs           one row per agent run, for the monitor to grade after the fact
    run_scratch    append-only shared memory between the planner and the executor
    scratch_reads  which scratch keys each agent actually READ (see T6.5)
    silences       every time the agent deliberately said nothing, and why
                   (HW3, T9.4)

`sqlite3` is stdlib, so this adds no dependency (CLAUDE.md §4).
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .uid import resolve_uid

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _REPO_ROOT / "store" / "radf.db"

TEAM = "team"


def db_path() -> Path:
    """Where the overlay lives.

    `RADF_DB_PATH` is read at CALL time so tests and demos can point at a
    temporary database without touching store/ — same rule the graph file
    follows (decision #11).
    """
    override = os.environ.get("RADF_DB_PATH")
    return Path(override) if override else _DEFAULT_DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    symbol_uid  TEXT,               -- NULL = repo-wide
    visibility  TEXT NOT NULL,      -- 'team' | 'user:<id>'
    author_id   TEXT NOT NULL,
    decision    TEXT NOT NULL,
    rationale   TEXT NOT NULL,
    rejected    TEXT,
    status      TEXT NOT NULL,      -- proposed | accepted | superseded
    supersedes  TEXT,               -- decision_id this replaces
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_uid ON decisions(symbol_uid);
CREATE INDEX IF NOT EXISTS idx_decisions_vis ON decisions(visibility);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    agent      TEXT NOT NULL,
    request    TEXT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    stopped    TEXT,
    log_path   TEXT
);

-- Append-only. A row is never UPDATEd or DELETEd: the history IS the audit
-- trail, and an overwritten value is exactly the bug this table is hard to
-- debug for (T6.5).
CREATE TABLE IF NOT EXISTS run_scratch (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    agent  TEXT NOT NULL,
    step   INTEGER NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,           -- json
    ts     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scratch_run ON run_scratch(run_id, key);

-- The other half of the causal chain: what was written is useless for debugging
-- without what was read.
CREATE TABLE IF NOT EXISTS scratch_reads (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    agent   TEXT NOT NULL,
    step    INTEGER NOT NULL,
    key     TEXT NOT NULL,
    saw_seq INTEGER,                -- run_scratch.seq actually observed, NULL = miss
    ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reads_run ON scratch_reads(run_id);

-- HW3 (T9.4). A deliberate non-answer is an OUTCOME, not an absence, and the
-- reason has to be readable later or "silence with a reason recorded" is just
-- silence. It carries its own `visibility` because the most important silence
-- we have — the private-decision leak guard (T11.2) — must be visible to the
-- OWNER of the withheld decision and to nobody else. A silence log that
-- everyone can read would announce exactly what the silence was protecting.
CREATE TABLE IF NOT EXISTS silences (
    silence_id  TEXT PRIMARY KEY,
    run_id      TEXT,               -- NULL when no run was ever opened
    trigger     TEXT NOT NULL,      -- telegram | github | heartbeat
    reason_code TEXT NOT NULL,      -- closed set, see channel.silence
    evidence    TEXT NOT NULL,      -- what was CHECKED, never what was withheld
    visibility  TEXT NOT NULL,      -- 'team' | 'user:<id>'
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_silences_vis ON silences(visibility);
CREATE INDEX IF NOT EXISTS idx_silences_reason ON silences(reason_code);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the overlay, creating the schema if needed."""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Idempotent schema creation."""
    conn.executescript(_SCHEMA)
    conn.commit()


# --- visibility ---------------------------------------------------------------

def visible_to(user_id: Optional[str]) -> tuple[str, list[str]]:
    """SQL fragment + params limiting rows to what `user_id` may see.

    **This is the trust boundary, and it lives here on purpose.** Scoping is
    enforced in the query, not in the prompt: user B's agent is never handed
    user A's rows alongside an instruction not to use them. An instruction is a
    request; a WHERE clause is a boundary.

    `user_id=None` (an unauthenticated/system read) sees team rows only.
    """
    if not user_id:
        return "visibility = ?", [TEAM]
    return "visibility IN (?, ?)", [TEAM, f"user:{user_id}"]


# --- decisions ----------------------------------------------------------------

def insert_decision(
    conn: sqlite3.Connection,
    *,
    component: Optional[str],
    decision: str,
    rationale: str,
    status: str,
    author_id: str,
    visibility: str = TEAM,
    rejected: Optional[str] = None,
    supersedes: Optional[str] = None,
) -> dict[str, Any]:
    """Insert one authored decision. Returns the stored row as a dict.

    `component` is normalised through `resolve_uid` on the way in, so nothing
    downstream ever sees a raw component string — that is what keeps the
    GitNexus migration a uid remap (ARCHITECTURE.md §6.1).
    """
    row = {
        "decision_id": f"d_{uuid.uuid4().hex[:12]}",
        "symbol_uid": resolve_uid(component),
        "visibility": visibility,
        "author_id": author_id,
        "decision": decision.strip(),
        "rationale": rationale.strip(),
        "rejected": (rejected or "").strip() or None,
        "status": status,
        "supersedes": supersedes,
        "ts": _now(),
    }
    conn.execute(
        "INSERT INTO decisions (decision_id, symbol_uid, visibility, author_id,"
        " decision, rationale, rejected, status, supersedes, ts)"
        " VALUES (:decision_id, :symbol_uid, :visibility, :author_id,"
        " :decision, :rationale, :rejected, :status, :supersedes, :ts)",
        row,
    )
    if supersedes:
        conn.execute(
            "UPDATE decisions SET status = 'superseded' WHERE decision_id = ?",
            (supersedes,),
        )
    conn.commit()
    return row


def query_decisions(
    conn: sqlite3.Connection,
    *,
    user_id: Optional[str],
    symbol_uids: Optional[Iterable[str]] = None,
    include_repo_wide: bool = True,
    statuses: Iterable[str] = ("accepted", "proposed"),
) -> list[dict[str, Any]]:
    """Decisions `user_id` may see, optionally narrowed to an impact set.

    This is the join the relational store exists for: given the modules a change
    touches, return the decisions that constrain them, in one query.

    Repo-wide decisions (`symbol_uid IS NULL`) are included by default — they
    apply everywhere, so a narrowed query that dropped them would silently lose
    the broadest constraints.
    """
    vis_sql, params = visible_to(user_id)
    where = [vis_sql]

    status_list = list(statuses)
    if status_list:
        where.append(f"status IN ({','.join('?' * len(status_list))})")
        params += status_list

    uids = list(symbol_uids) if symbol_uids is not None else None
    if uids is not None:
        clause = f"symbol_uid IN ({','.join('?' * len(uids))})" if uids else "0"
        if include_repo_wide:
            clause = f"({clause} OR symbol_uid IS NULL)"
        where.append(clause)
        params += uids

    rows = conn.execute(
        f"SELECT * FROM decisions WHERE {' AND '.join(where)} ORDER BY ts ASC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def decisions_across_scopes(
    conn: sqlite3.Connection,
    *,
    symbol_uids: Iterable[str],
    statuses: Iterable[str] = ("accepted", "proposed"),
) -> list[dict[str, Any]]:
    """Decisions on `symbol_uids` **ignoring visibility**. Read the whole docstring.

    This is the one query in the system that crosses the scope boundary, and it
    exists for exactly one caller: the private-decision leak guard
    (`channel.silence.evaluate_silence`, T11.2). That guard has to compare what a
    user CAN see against what EXISTS, and it cannot make that comparison from the
    filtered side alone.

    **Its results must never reach a model, a channel, or a log.** The guard
    returns a decision, not text; these rows die on its stack frame. Anything
    else in the system that wants decisions calls `query_decisions`, which
    filters (decision #24).

    Kept deliberately awkward to reach — a distinct name, not a flag on
    `query_decisions` — because `query_decisions(..., all_scopes=True)` is one
    autocomplete away from being the default in a hurry.
    """
    uids = [u for u in symbol_uids if u]
    if not uids:
        return []
    status_list = list(statuses)
    params: list[str] = list(uids)
    where = [f"symbol_uid IN ({','.join('?' * len(uids))})"]
    if status_list:
        where.append(f"status IN ({','.join('?' * len(status_list))})")
        params += status_list
    rows = conn.execute(
        f"SELECT * FROM decisions WHERE {' AND '.join(where)} ORDER BY ts ASC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def all_decision_uids(conn: sqlite3.Connection) -> list[str]:
    """Every non-null `symbol_uid` in the overlay — for the orphan check."""
    rows = conn.execute(
        "SELECT DISTINCT symbol_uid FROM decisions WHERE symbol_uid IS NOT NULL"
    ).fetchall()
    return [r["symbol_uid"] for r in rows]


def import_legacy_decisions(conn: sqlite3.Connection, graph: dict) -> int:
    """One-time migration of HW1's `decisions[]` out of the JSON graph (T4.3).

    Idempotent: a record already present (same uid + text) is skipped, so
    re-running is safe. Imported rows are `visibility='team'` and
    `author_id='hw1'` — HW1 had no notion of either, and team is the
    conservative reading of a decision written before scoping existed.

    Returns the number of rows actually inserted.
    """
    legacy = graph.get("decisions") or []
    inserted = 0
    for record in legacy:
        if not isinstance(record, dict):
            continue
        uid = resolve_uid(record.get("component"))
        text = (record.get("decision") or "").strip()
        if not text:
            continue
        existing = conn.execute(
            "SELECT 1 FROM decisions WHERE symbol_uid IS ? AND decision = ?",
            (uid, text),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO decisions (decision_id, symbol_uid, visibility,"
            " author_id, decision, rationale, rejected, status, supersedes, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)",
            (
                f"d_{uuid.uuid4().hex[:12]}",
                uid,
                TEAM,
                "hw1",
                text,
                (record.get("rationale") or "").strip(),
                record.get("status") or "accepted",
                record.get("ts") or _now(),
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


# --- runs ---------------------------------------------------------------------

def start_run(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    thread_id: str,
    agent: str,
    request: str = "",
    log_path: str = "",
) -> str:
    """Open a run row and return its `run_id`."""
    run_id = f"r_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO runs (run_id, user_id, thread_id, agent, request,"
        " started_at, log_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, user_id, thread_id, agent, request, _now(), log_path),
    )
    conn.commit()
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, stopped: str) -> None:
    """Close a run row with the loop's stopping condition."""
    conn.execute(
        "UPDATE runs SET ended_at = ?, stopped = ? WHERE run_id = ?",
        (_now(), stopped, run_id),
    )
    conn.commit()


# --- shared scratch (planner <-> executor) ------------------------------------

def scratch_write(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    agent: str,
    step: int,
    key: str,
    value: Any,
) -> int:
    """Append one entry to the shared scratch. Never overwrites. Returns its seq.

    Append-only is not fussiness. Shared memory between agents is the hardest
    coordination to debug precisely because it is a channel with no call site —
    neither agent's code shows the dependency, so an executor behaving oddly can
    be caused by something the planner wrote three steps earlier. If writes
    overwrote, the evidence would be gone by the time you looked.
    """
    cur = conn.execute(
        "INSERT INTO run_scratch (run_id, agent, step, key, value, ts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, agent, step, key, json.dumps(value, default=str), _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def scratch_read(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    agent: str,
    step: int,
    key: str,
) -> Any:
    """Read the latest scratch value for `key`, RECORDING the read.

    The read is logged even when it misses (`saw_seq IS NULL`), because "the
    executor looked for the plan and found nothing" is exactly the trace you
    want when debugging a coordination bug.
    """
    row = conn.execute(
        "SELECT seq, value FROM run_scratch WHERE run_id = ? AND key = ?"
        " ORDER BY seq DESC LIMIT 1",
        (run_id, key),
    ).fetchone()
    conn.execute(
        "INSERT INTO scratch_reads (run_id, agent, step, key, saw_seq, ts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, agent, step, key, row["seq"] if row else None, _now()),
    )
    conn.commit()
    if row is None:
        return None
    return json.loads(row["value"])


# --- silences (HW3, T9.4) -----------------------------------------------------

def record_silence(
    conn: sqlite3.Connection,
    *,
    trigger: str,
    reason_code: str,
    evidence: str,
    visibility: str = TEAM,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Write down that the agent chose to say nothing. Returns the stored row.

    Silence is the one outcome that leaves no trace anywhere else — no reply in
    the channel, often no run record, nothing for the monitor to grade. If it is
    not written here it did not happen as far as anyone can tell afterwards,
    which makes a deliberate non-answer indistinguishable from a crashed worker.

    `evidence` is the caller's contract to keep: it records what was CHECKED
    (uids, counts, who asked), never the content that was withheld. Nothing in
    this function can enforce that, which is why it is stated in
    `channel.silence.SilenceDecision` as well.
    """
    row = {
        "silence_id": f"s_{uuid.uuid4().hex[:12]}",
        "run_id": run_id,
        "trigger": trigger,
        "reason_code": reason_code,
        "evidence": evidence,
        "visibility": visibility,
        "ts": _now(),
    }
    conn.execute(
        "INSERT INTO silences (silence_id, run_id, trigger, reason_code,"
        " evidence, visibility, ts) VALUES (:silence_id, :run_id, :trigger,"
        " :reason_code, :evidence, :visibility, :ts)",
        row,
    )
    conn.commit()
    return row


def query_silences(
    conn: sqlite3.Connection,
    *,
    user_id: Optional[str],
    reason_code: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Silences `user_id` may see, newest first.

    Filtered through the same `visible_to` fragment as decisions — **in the
    query, never after** (decision #24). The private-decision guard depends on
    this: its record names the owner's scope, so the owner learns somebody went
    looking and the asker learns nothing, including that there was anything to
    learn.
    """
    vis_sql, params = visible_to(user_id)
    where = [vis_sql]
    if reason_code:
        where.append("reason_code = ?")
        params = params + [reason_code]

    rows = conn.execute(
        f"SELECT * FROM silences WHERE {' AND '.join(where)}"
        " ORDER BY ts DESC, rowid DESC LIMIT ?",
        params + [int(limit)],
    ).fetchall()
    return [dict(r) for r in rows]


def count_silences(conn: sqlite3.Connection, *, user_id: Optional[str]) -> int:
    """How many silences `user_id` may see. Same filter, same rule."""
    vis_sql, params = visible_to(user_id)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM silences WHERE {vis_sql}", params
    ).fetchone()
    return int(row["n"])


def scratch_dump(conn: sqlite3.Connection, run_id: str) -> dict[str, list[dict]]:
    """Full write + read history for one run — what the judge and a human read."""
    writes = conn.execute(
        "SELECT * FROM run_scratch WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall()
    reads = conn.execute(
        "SELECT * FROM scratch_reads WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall()
    return {
        "writes": [dict(w) for w in writes],
        "reads": [dict(r) for r in reads],
    }
