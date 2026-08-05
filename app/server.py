"""app/server.py — the demo web app: a red button, a meme, and a live trace panel.

Owner: Berat Furkan Kocak (final demo).

**This is an observer. It changes nothing.** It imports no agent code, holds no
session, and writes to no store. Everything it shows is read from two files the
system already produces:

  app/logs/service.log   service.py's own stdout (run with `python -u`), tailed
  store/runs/runs.jsonl  the RunLog every finished turn flushes (agentlib.runlog)

Two sources, because they answer different questions and neither answers both.
stdout is LIVE but coarse — it tells you a turn started and which tools were
offered. runs.jsonl is EXACT but late — it lands when the turn ends, and it is
the only place the actual tool calls, their branches, and the planner's
`impacted` set are recorded. A demo needs both: the live line so the panel moves
while the agent is thinking, the run record so what it moved for is true.

Stdlib only. `http.server` is not a production server and this is not a
production app; adding a web framework to draw one button would be exactly the
"dependency added for convenience" CLAUDE.md §4 refuses.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
MEMES_DIR = APP_DIR / "memes"
STATIC_DIR = APP_DIR / "static"
SERVICE_LOG = APP_DIR / "logs" / "service.log"
RUNS_FILE = REPO_ROOT / "store" / "runs" / "runs.jsonl"

# So `retrieval.store` is importable for the index status line. This is the only
# repo module the app touches, and only to read a table name and a DSN.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
MAX_EVENTS = 400          # the panel is a window, not an archive
POLL_SECONDS = 0.4


# --- the event log ------------------------------------------------------------
#
# One list, one lock, two producers. `kind` drives the colour in the panel and is
# the only thing the frontend branches on, so it stays a small closed set:
#
#   boot     startup / infrastructure check          (header section)
#   user     an inbound message from a human
#   agent    which agent picked the turn up
#   tool     a tool call the agent made, and how it went
#   plan     the planner's output — impact scope, constraints, questions
#   exec     the handover to the executor and what it did
#   gate     an approval prompt for an irreversible action
#   answer   what went back to the human
#   note     anything else worth seeing
#   error    a failure
#
# `actor` is orthogonal to `kind` and answers the other question a viewer has —
# *who* did this. It matters because the system is three different things wearing
# one log: a single read-only agent on the question path, and a planner and an
# executor on the change path. "A tool was called" is much less useful than "the
# EXECUTOR called a tool", especially at the moment the plan is handed over.
#
#   system    the process itself — boot, queue, transport
#   user      a human
#   qa-agent  the channel's single read-only Q&A agent (the ONLY agent on
#             the question path — there is no planner or executor there)
#   planner   agents.planner
#   executor  agents.executor
#   gate      the approval gate (nobody's agent — it is the human's turn)
#
# Attribution is structural, never guessed: stdout lines carry their stage in the
# prefix service.py already prints, and a run record's steps belong to the
# executor when the run came from the orchestrator and to the channel agent
# otherwise — the planner makes no `run_agent` tool calls at all (#66).
ACTORS = ("system", "user", "qa-agent", "planner", "executor", "gate")


def _unrepr(text: str) -> str:
    """Strip the quotes `service.py`'s `!r` adds, and the ellipsis if truncated."""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text.rstrip("…").strip()


class EventLog:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, kind: str, text: str, *, detail: str = "", section: str = "chat",
            actor: str = "system") -> None:
        with self._lock:
            self._seq += 1
            self._events.append({
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "section": section,
                "kind": kind,
                "actor": actor,
                "text": text,
                "detail": detail,
            })
            if len(self._events) > MAX_EVENTS:
                del self._events[:len(self._events) - MAX_EVENTS]

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    def already_showed_user(self, text: str) -> bool:
        """Did the live tailer already print this inbound message?

        Both sources carry the human's message: stdout the moment it arrives,
        the run record when the turn ends. The live one is the useful one — it
        is what makes the panel move while the agent thinks — so the record's
        copy is suppressed rather than printed a second time under the answer
        it produced.

        Compared on a **prefix**, not for equality: `service.py` prints
        `event.text[:80]!r`, so the live copy is repr-quoted and truncated while
        the run record holds the message in full. Equality silently never
        matched, and the symptom was every message appearing twice — which reads
        as a redundant panel rather than as a broken comparison.
        """
        needle = _unrepr(text)
        with self._lock:
            for event in reversed(self._events):
                if event["kind"] == "user":
                    seen = _unrepr(event["text"])
                    return bool(seen) and (needle.startswith(seen)
                                           or seen.startswith(needle))
        return False


LOG = EventLog()


# --- infrastructure checks ----------------------------------------------------

def _run(cmd: list[str], timeout: float = 6.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or proc.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out"
    except Exception as exc:  # noqa: BLE001 — a status check must never take the page down
        return 1, str(exc)


def check_docker() -> dict[str, Any]:
    code, out = _run(["docker", "inspect", "-f", "{{.State.Running}}", "radf-pgvector"])
    running = code == 0 and out.strip() == "true"
    return {"name": "docker · radf-pgvector",
            "ok": running,
            "detail": "container running" if running else (out or "not running")}


def check_postgres() -> dict[str, Any]:
    code, out = _run(["docker", "exec", "radf-pgvector", "pg_isready", "-U", "radf", "-d", "radf"])
    ok = code == 0
    return {"name": "postgres · localhost:5433",
            "ok": ok,
            "detail": out.split("-")[-1].strip() if ok else "not accepting connections"}


def check_index() -> dict[str, Any]:
    """How many chunks the retrieval index holds, straight from Postgres.

    Read-only and best-effort: `psycopg` may not be installed on the host running
    the demo page, and that is a degraded status line, not a crash.
    """
    try:
        import psycopg  # noqa: PLC0415 — optional, checked at call time on purpose

        from retrieval.store import TABLE, dsn  # type: ignore
        with psycopg.connect(dsn(), connect_timeout=4) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT kind, count(*) FROM {TABLE} GROUP BY kind ORDER BY 1")
                rows = cur.fetchall()
        total = sum(int(n) for _, n in rows)
        parts = ", ".join(f"{n} {kind}" for kind, n in rows) or "empty"
        return {"name": "retrieval index · pgvector",
                "ok": total > 0,
                "detail": f"{total} chunks ({parts})" if total else
                          "empty — run: python -m retrieval.index --rebuild"}
    except Exception as exc:  # noqa: BLE001
        return {"name": "retrieval index · pgvector", "ok": False,
                "detail": f"unreadable ({type(exc).__name__})"}


def check_overlay() -> dict[str, Any]:
    db = REPO_ROOT / "store" / "radf.db"
    graph = REPO_ROOT / "store" / "knowledge_graph.json"
    ok = db.exists() and graph.exists()
    bits = []
    if db.exists():
        bits.append(f"overlay {db.stat().st_size // 1024} KB")
    if graph.exists():
        try:
            data = json.loads(graph.read_text(encoding="utf-8"))
            bits.append(f"graph {len(data.get('nodes') or [])} nodes")
        except Exception:  # noqa: BLE001
            bits.append("graph unreadable")
    return {"name": "store · sqlite overlay + graph", "ok": ok,
            "detail": ", ".join(bits) or "missing"}


def check_service() -> dict[str, Any]:
    """Is service.py alive, and did it reach Telegram?

    Reads the tail of its own log rather than tracking a pid: the log line
    `[service] connected as @bot` is the only evidence that the *token* works,
    which is the part that actually breaks before a demo.
    """
    if not SERVICE_LOG.exists():
        return {"name": "telegram · service.py", "ok": False, "detail": "no log yet"}
    text = _tail(SERVICE_LOG, 8000)
    handle = re.search(r"\[service\] connected as (@\S+)", text)
    listening = "[service] listening" in text
    if handle and listening:
        return {"name": "telegram · service.py", "ok": True,
                "detail": f"listening as {handle.group(1)}"}
    if handle:
        return {"name": "telegram · service.py", "ok": True,
                "detail": f"connected as {handle.group(1)}"}
    return {"name": "telegram · service.py", "ok": False,
            "detail": "not connected — check TELEGRAM_BOT_TOKEN"}


def _tail(path: Path, nbytes: int) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - nbytes))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def status_payload() -> dict[str, Any]:
    checks = [check_docker(), check_postgres(), check_index(),
              check_overlay(), check_service()]
    return {"checks": checks, "ok": all(c["ok"] for c in checks)}


# --- tailer 1: service.py stdout ----------------------------------------------

# Only lines that mean something to a viewer. Everything else in the stream is
# framework noise, and a panel that shows everything shows nothing.
#
# The two sources overlap, and where they do this one yields: the run record
# carries the same turn with the tool calls, their branches and the planner's
# actual `impacted` list, so replaying stdout's coarser version of it prints
# every turn twice. Patterns mapped to `DROP` are the ones runs.jsonl says
# better. What stays is either LIVE-ONLY — the turn is starting, a gate is
# waiting; things a viewer needs before the run record exists — or STDOUT-ONLY,
# which is the planner's retrieval and the seed/hop cap.
DROP = "drop"

# (pattern, kind, actor, template)
_STDOUT_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"^\[service\] (.+)$"), "boot", "system", r"\1"),
    # Live: the message lands here seconds before the run record exists. MUST sit
    # ahead of the generic `[telegram]` pattern below — first match wins, and
    # this line is the more specific case of it. When it did not, every inbound
    # message rendered as a system note and `already_showed_user` never fired,
    # so the run record printed the message again a few seconds later.
    (re.compile(r"^\[(telegram|github|heartbeat)\] (.+?) — (.+)$"), "user", "user", r"\3"),
    # Transport noise from channel/telegram.py, which prints indented — the
    # leading `\s*` is what distinguishes it from the un-indented line above.
    (re.compile(r"^\s+\[telegram\] (.+)$"), "note", "system", r"telegram: \1"),
    (re.compile(r"^\s*\[QUEUE\] (.+)$"), "note", "system", r"queue: \1"),
    (re.compile(r"^\s*\[SILENT\] (.+)$"), "note", "system", r"stayed silent: \1"),
    # Live and load-bearing: a gate is a question somebody has to answer NOW.
    # Attributed to `gate` rather than to the executor that tripped it — at this
    # moment it is the human's turn, and that is the point of the row.
    (re.compile(r"^\s*\[gate->(.+?)\] (.+)$"), "gate", "gate", r"approval asked: \2"),
    (re.compile(r"^\s*\[GATE\] (.+)$"), "gate", "gate", r"approval asked: \1"),
    # Stdout-only, and the planner's own. A real `search_corpus` call with real
    # args and real results — made by Python rather than chosen by the model
    # (#66), which is why it reaches no run_agent trace. Rendered in the `tool`
    # lane because that is what it is; the detail names who decided to make it,
    # so the panel shows the step without claiming the model picked it.
    (re.compile(r"^\s*\[retrieval\] (.+)$"), "tool", "planner", r"\1"),
    # The impact walk and the decision lookups, same story: real calls to the
    # same tools the loop offers, made by Python so the model cannot be talked
    # out of the hop cap (#34), and therefore in no run_agent trace either.
    # Without these the plan showed `impacted=2` with nothing to say where the 2
    # came from — "the walk found one dependent" and "the walk never ran" looked
    # identical.
    (re.compile(r"^\s*\[graph\] (.+)$"), "tool", "planner", r"\1"),
    (re.compile(r"^\s*\[decisions\] (.+)$"), "tool", "planner", r"\1"),
    # Stdout-only: the seed and the hop cap. `impacted=N` is the count the plan
    # envelope later itemises, and the cap is #34's "visible in the trace".
    (re.compile(r"^\[planner\] (.+)$"), "plan", "planner", r"\1"),
    # Live: the handover beat. The executor's own tool calls arrive with the run
    # record, so only the "starting" line earns a row.
    (re.compile(r"^\[EXECUTOR\] implementing\s*$"), "exec", "executor",
     "picking up the plan"),
    (re.compile(r"^\s*\[ERROR\] (.+)$"), "error", "system", r"\1"),
    (re.compile(r"^\s*\[silence\] (.+)$"), "note", "system", r"silence policy: \1"),

    # --- said better by the run record ---
    (re.compile(r"^\s*\[TOOLS\] offered: "), DROP, "", ""),   # a registry, not a step
    (re.compile(r"^\s*\[STOPPED\] "), DROP, "", ""),          # -> run.stopped
    (re.compile(r"^\[PLANNER\] "), DROP, "", ""),             # -> the plan envelope
    (re.compile(r"^\[EXECUTOR\] "), DROP, "", ""),            # -> the executor envelope
]


def classify_stdout(line: str) -> Optional[tuple[str, str, str]]:
    """`(kind, actor, text)` for a line worth showing; None for noise/duplicates.

    First match wins, so the specific patterns above sit ahead of the broad
    `DROP` prefixes they carve exceptions out of.
    """
    for pattern, kind, actor, template in _STDOUT_PATTERNS:
        match = pattern.match(line.rstrip())
        if match:
            return None if kind is DROP else (kind, actor, match.expand(template))
    return None


def tail_service_log(stop: threading.Event) -> None:
    """Follow service.py's stdout from wherever it is now.

    Starts at end-of-file: the panel is about this demo run, not about whatever
    the log accumulated last week.
    """
    pos = SERVICE_LOG.stat().st_size if SERVICE_LOG.exists() else 0
    while not stop.is_set():
        try:
            if SERVICE_LOG.exists():
                size = SERVICE_LOG.stat().st_size
                if size < pos:            # the script truncated it on restart
                    pos = 0
                if size > pos:
                    with SERVICE_LOG.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        chunk = fh.read()
                        pos = fh.tell()
                    for line in chunk.splitlines():
                        hit = classify_stdout(line)
                        if hit:
                            kind, actor, text = hit
                            # `call -> result` renders in the same two-part shape
                            # as a tool row out of runs.jsonl, so a line from
                            # stdout and one from the run record look alike.
                            text, _, detail = text.partition(" -> ")
                            if kind == "tool" and detail:
                                detail += "   [code-owned, not a model choice]"
                            if kind == "user":
                                text = _unrepr(text)   # service.py prints it !r
                            LOG.add(kind, text, detail=detail, actor=actor,
                                    section="boot" if kind == "boot" else "chat")
        except OSError:
            pass
        stop.wait(POLL_SECONDS)


# --- tailer 2: store/runs/runs.jsonl ------------------------------------------

def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# What `search_corpus`'s `score` actually is, when the reranker ran: the value
# of the named band the reranker put the chunk in (retrieval/rerank.py BANDS),
# NOT a similarity. So the panel prints the band name.
#
# Showing "1.0" was actively misleading in two directions: it reads as "100%
# match" when it means "the reranker judged this as answering the question", and
# a whole result list of 1.0s reads as a broken scorer when it means all five
# candidates landed in the top band — which is the reranker working. It also
# contradicted the design it was displaying: decision #65 chose three named
# bands over a numeric score precisely so a human could audit the judgement, and
# rendering the band back as a float threw that away at the last step.
_BANDS = {1.0: "answers", 0.5: "related", 0.0: "unrelated"}


def _score_label(row: dict[str, Any], reranked: bool) -> str:
    score = row.get("score")
    if not isinstance(score, (int, float)):
        return ""
    if reranked:
        return _BANDS.get(round(float(score), 3), f"band {score}")
    # Not reranked: this is the RRF fusion score — a small float whose absolute
    # value means nothing on its own, only its order. Labelled so it cannot be
    # mistaken for a band or a percentage.
    return f"rrf {score:.4f}"


def _tool_summary(step: dict[str, Any]) -> tuple[str, str]:
    """One line per tool call: what was asked, and what came back.

    `search_corpus` gets its own shape because a ranked result list is the whole
    point of it — showing `{"results": [...]}` truncated would hide exactly the
    thing worth watching.
    """
    name = step.get("tool") or "?"
    args = step.get("args") or {}
    out = step.get("output")
    branch = step.get("branch") or "ok"

    call = f"{name}({', '.join(f'{k}={_short(v, 48)}' for k, v in args.items())})"

    if isinstance(out, dict) and out.get("error"):
        return call, f"error: {out['error']}"
    if name == "search_corpus" and isinstance(out, dict):
        hits = out.get("results") or []
        reranked = bool(out.get("reranked"))
        top = "; ".join(
            f"#{h.get('rank')} "
            f"{h.get('symbol') or h.get('heading_path') or h.get('chunk_id')}"
            + (f" [{lbl}]" if (lbl := _score_label(h, reranked)) else "")
            for h in hits[:3]
        )
        tag = "reranked" if reranked else "fused (no rerank)"
        return call, f"{out.get('count', len(hits))} passages, {tag} — {top or 'none'}"
    if name == "query_component_graph" and isinstance(out, dict):
        node = (out.get("node") or {}).get("id")
        rel = out.get("related") or out.get("edges") or []
        return call, (f"{node or 'not found'} — {len(rel)} related"
                      if out.get("found") else "not in graph")
    if name == "retrieve_decisions" and isinstance(out, dict):
        return call, f"{len(out.get('decisions') or [])} decision(s) on file"
    if branch != "ok":
        return call, branch
    return call, _short(out, 140)


def _emit_envelope(entry: dict[str, Any]) -> None:
    who = entry.get("agent") or "?"
    env = entry.get("envelope") or {}
    result = env.get("result") or {}
    status = env.get("status")

    if who == "planner":
        impacted = result.get("impacted") or []
        questions = result.get("open_questions") or []
        LOG.add("plan", f"plan {status} — impact_scope: {len(impacted)} component(s)",
                actor="planner",
                detail=(", ".join(impacted[:6]) or "none")
                       + f"  ·  constraints: {len(result.get('constraints') or [])}"
                       + (f"  ·  max_hops: {result.get('impact_max_hops')}"
                          if result.get("impact_max_hops") is not None else ""))
        if questions:
            LOG.add("gate", f"stopped to ask {len(questions)} question(s)",
                    actor="planner", detail=_short(questions[0], 160))
    elif who == "executor":
        changes = result.get("changes") or []
        LOG.add("exec", f"finished — {status}, {len(changes)} file(s) changed",
                actor="executor",
                detail=", ".join(str(c.get("path")) for c in changes) or
                       _short(env.get("notes") or "", 160))
    else:
        LOG.add("note", f"{who}: {status}", actor=who if who in ACTORS else "system",
                detail=_short(env.get("notes") or ""))


def expand_run(run: dict[str, Any]) -> None:
    """Turn one finished RunLog record into panel events, in the order it happened.

    The record itself is not in narrative order: a `/change` run carries ONE
    `run_log` shared by both agents, so `steps` holds the executor's tool calls
    while the planner's envelope — which produced the plan those calls implement
    — sits later in `envelopes`. Replaying the file's order shows the executor
    acting before the plan exists. So the planner's envelope is emitted first,
    then the tool calls, then the executor's.
    """
    agent = run.get("agent") or "agent"
    user = run.get("user_id") or "anonymous"
    request = (run.get("request") or "").strip()
    envelopes = run.get("envelopes") or []

    # Whose tool calls are in `steps`. Structural, not guessed: on the change
    # path the orchestrator shares ONE run_log between both agents, and only the
    # executor runs `run_agent` — the planner's own retrieval is a direct Python
    # call that never enters a trace (#66). Any other run is the channel's single
    # read-only Q&A agent (or the CLI's), which is one agent, not two.
    from_orchestrator = agent == "orchestrator"
    step_actor = "executor" if from_orchestrator else "qa-agent"

    if request and not LOG.already_showed_user(request):
        LOG.add("user", request, actor="user",
                detail=f"user={user}  run={run.get('run_id')}")
    LOG.add("agent",
            "change request — planner then executor" if from_orchestrator
            else f"question — single read-only agent ({agent})",
            actor="system",
            detail=f"user={user}  thread={run.get('thread_id')}  run={run.get('run_id')}")

    for entry in envelopes:
        if (entry.get("agent") or "") == "planner":
            _emit_envelope(entry)

    if from_orchestrator and (run.get("steps") or []):
        LOG.add("exec", "plan handed over — executor takes it from here",
                actor="executor")

    for step in run.get("steps") or []:
        call, result = _tool_summary(step)
        branch = step.get("branch") or "ok"
        # A declined gate is not a failure — it is the gate doing its job, and
        # colouring it red teaches a demo audience the wrong lesson.
        kind = {"ok": "tool", "declined": "gate"}.get(branch, "error")
        LOG.add(kind, call, detail=result,
                actor="gate" if branch == "declined" else step_actor)

    for entry in envelopes:
        if (entry.get("agent") or "") != "planner":
            _emit_envelope(entry)

    answer = run.get("answer")
    if answer:
        LOG.add("answer", _short(answer, 400),
                actor="executor" if from_orchestrator else "qa-agent",
                detail=f"stopped: {run.get('stopped')}")
    else:
        LOG.add("note", f"turn ended: {run.get('stopped')}", actor=step_actor)


def tail_runs(stop: threading.Event) -> None:
    """Follow runs.jsonl from end-of-file. One JSON object per finished turn."""
    pos = RUNS_FILE.stat().st_size if RUNS_FILE.exists() else 0
    buffer = ""
    while not stop.is_set():
        try:
            if RUNS_FILE.exists():
                size = RUNS_FILE.stat().st_size
                if size < pos:
                    pos, buffer = 0, ""
                if size > pos:
                    with RUNS_FILE.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        buffer += fh.read()
                        pos = fh.tell()
                    # A record may still be mid-write; keep the trailing partial.
                    *lines, buffer = buffer.split("\n")
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            expand_run(json.loads(line))
                        except (ValueError, TypeError):
                            LOG.add("error", "unparseable run record", detail=_short(line))
        except OSError:
            pass
        stop.wait(POLL_SECONDS)


# --- memes --------------------------------------------------------------------

# --- what the agent knows about this app --------------------------------------
#
# The "what does it know?" panel. It reads the same two layers `search_corpus`
# reads and shows them side by side, because the join between them is the whole
# architecture (#5, #57): the GRAPH says the module exists (derived, any scan may
# replace it), the OVERLAY says what it is for (authored, no scan may touch it),
# and they meet on `symbol_uid` — never merged into one store.
#
# The card text shown here is not a paraphrase of the chunk. It is fetched from
# Postgres by `symbol_uid`, so it is byte-for-byte the passage `search_corpus`
# would return for a hit on this module.

def app_modules() -> list[dict[str, Any]]:
    """Every `app.*` node in the knowledge graph, with its summary card count."""
    graph = REPO_ROOT / "store" / "knowledge_graph.json"
    try:
        data = json.loads(graph.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    from overlay import db as overlay_db  # noqa: PLC0415 — after sys.path setup

    conn = overlay_db.connect()
    try:
        nodes = [
            n for n in (data.get("nodes") or [])
            if isinstance(n, dict) and str(n.get("id", "")).split(".")[0] == "app"
        ]
        out = []
        for node in sorted(nodes, key=lambda n: str(n.get("id"))):
            uid = f"Module:{node['id']}"
            cards = overlay_db.query_node_summaries(conn, symbol_uids=[uid])
            out.append({
                "id": node["id"],
                "uid": uid,
                "path": node.get("path"),
                "symbols": node.get("symbols") or [],
                "cards": len(cards),
            })
        return out
    finally:
        conn.close()


def module_detail(uid: str) -> dict[str, Any]:
    """The authored cards for one module, plus the chunks they became."""
    from overlay import db as overlay_db  # noqa: PLC0415

    conn = overlay_db.connect()
    try:
        cards = overlay_db.query_node_summaries(conn, symbol_uids=[uid])
    finally:
        conn.close()

    chunks: list[dict[str, Any]] = []
    indexed_error = None
    try:
        import psycopg  # noqa: PLC0415

        from retrieval.store import TABLE, dsn  # type: ignore
        with psycopg.connect(dsn(), connect_timeout=4) as conn2, conn2.cursor() as cur:
            cur.execute(
                f"SELECT chunk_id, kind, symbol, heading_path, text FROM {TABLE}"
                " WHERE symbol_uid = %s ORDER BY symbol NULLS FIRST", (uid,))
            chunks = [
                {"chunk_id": r[0], "kind": r[1], "symbol": r[2],
                 "heading_path": r[3], "text": r[4]}
                for r in cur.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001
        indexed_error = f"{type(exc).__name__} — is Postgres up?"

    return {
        "uid": uid,
        "cards": [
            {"symbol": c["symbol"] or "(module)",
             "summary": c["summary"],
             "responsibility": c["responsibility"],
             "signature": c["signature"],
             "author_id": c["author_id"],
             "content_sha": (c["content_sha"] or "")[:12],
             "updated_at": c["updated_at"]}
            for c in cards
        ],
        "chunks": chunks,
        "indexed_error": indexed_error,
    }


def list_memes() -> list[str]:
    if not MEMES_DIR.is_dir():
        return []
    return sorted(p.name for p in MEMES_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


# --- http ---------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "BillionDollarStartup/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # the demo's log panel is the log; access lines would drown it

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        path = unquote(urlparse(self.path).path)
        query = urlparse(self.path).query

        if path in ("/", "/index.html"):
            # Re-read and re-substitute per request, so an agent that edits
            # app/theme.py shows up on the next reload with no restart — which
            # is the whole point of the `/change` half of the demo.
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            import importlib  # noqa: PLC0415 — reloaded on purpose, see above

            from app import theme  # type: ignore
            importlib.reload(theme)
            for token, value in theme.as_substitutions().items():
                html = html.replace(token, value)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/status":
            self._json(status_payload())
            return

        if path == "/api/logs":
            since = 0
            for part in query.split("&"):
                if part.startswith("since="):
                    try:
                        since = int(part[6:])
                    except ValueError:
                        since = 0
            self._json({"events": LOG.since(since)})
            return

        if path == "/api/modules":
            self._json({"modules": app_modules()})
            return

        if path == "/api/module":
            uid = ""
            for part in query.split("&"):
                if part.startswith("uid="):
                    uid = unquote(part[4:])
            if not uid.startswith("Module:app"):
                # The panel is about this app. Anything else is a request for a
                # module it has no business rendering, so it is refused rather
                # than served — the endpoint reads the overlay, and the overlay
                # holds rows this page is not the right surface for.
                self._json({"error": "out_of_scope",
                            "detail": "this panel serves app.* modules only"}, 400)
                return
            self._json(module_detail(uid))
            return

        if path == "/api/meme":
            names = list_memes()
            if not names:
                self._json({"error": "no_memes",
                            "hint": "drop images into app/memes/"}, 404)
                return
            self._json({"name": random.choice(names), "total": len(names)})
            return

        if path.startswith("/memes/"):
            name = Path(path[len("/memes/"):]).name   # basename only: no traversal
            target = MEMES_DIR / name
            if not target.is_file() or target.suffix.lower() not in IMAGE_SUFFIXES:
                self._send(404, b"not found", "text/plain")
                return
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)
            return

        self._send(404, b"not found", "text/plain")


def seed_boot_events() -> None:
    """The header section: what is up, checked live, before anything else prints."""
    LOG.add("boot", "billion dollar startup — booting", section="boot", actor="system")
    for check in status_payload()["checks"]:
        LOG.add("boot" if check["ok"] else "error",
                f"{'up' if check['ok'] else 'DOWN'}  ·  {check['name']}",
                detail=check["detail"], section="boot", actor="system")
    LOG.add("boot", f"{len(list_memes())} meme(s) in app/memes/",
            section="boot", actor="system")
    LOG.add("boot", "waiting for a Telegram message…", section="boot", actor="system")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RADF demo web app.")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    (APP_DIR / "logs").mkdir(exist_ok=True)
    seed_boot_events()

    stop = threading.Event()
    for target in (tail_service_log, tail_runs):
        threading.Thread(target=target, args=(stop,), daemon=True,
                         name=target.__name__).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[app] http://{args.host}:{args.port}  (memes: {MEMES_DIR})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[app] stopping", flush=True)
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
