"""overlay.memory — free-form memory the agent writes in its own words.

Owner: Berat Furkan Kocak (HW2, T4.4 / T4.5 / T5.6).

A JSON document store, deliberately not the SQLite next door. The split is not
"structured vs unstructured" — it is **whether the query is the product**.
Decisions are joined against an impact set, so they are relational. A learned
fact is retrieved by keyword and recency and has no stable shape, so it is a
document. Slides, Session 4: "No vectors, no embeddings, no RAG. Selective
loading means keyword and recency."

Two kinds live here:

    fact   something learned that would otherwise be re-asked. Saved WITH A CUE,
           which is how it comes back. When it surfaces, the MODEL decides what
           to do with it.
    rule   changes how the agent behaves. The model does not decide what to do —
           the rule already says. It decides only WHEN the rule applies, and
           mostly it does not even do that: a rule with `applies_to` set is
           bound mechanically by the impact set (see agentlib.context, T5.4).

Everything in this store is quoted user text. It carries `source.quoted = True`
and is rendered as data, never into `instructions` — a saved "user fact" that
reaches developer authority is the memory-injection attack from the slides.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MEMORY_PATH = _REPO_ROOT / "store" / "memory.json"

TEAM = "team"
_WORD = re.compile(r"[a-z0-9_./]+")


def memory_path() -> Path:
    """Where free-form memory lives. `RADF_MEMORY_PATH` is read at call time."""
    override = os.environ.get("RADF_MEMORY_PATH")
    return Path(override) if override else _DEFAULT_MEMORY_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> list[dict[str, Any]]:
    path = memory_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Same refusal as the graph file (decision #12): a corrupt store is
        # never silently recreated, because that would destroy authored memory.
        raise MemoryStoreCorrupt(f"{path} exists but is not valid JSON")
    return data if isinstance(data, list) else []


def _save(records: list[dict[str, Any]]) -> None:
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class MemoryStoreCorrupt(RuntimeError):
    """Raised on an unreadable memory file. Tool wrappers turn this into an
    error branch; it must never be swallowed into a silent rewrite."""


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _visible(record: dict, user_id: Optional[str]) -> bool:
    """The trust boundary, applied before anything reaches the model.

    Team memory is visible to everyone. Private memory is visible only to its
    owner. There is no prompt involved: B's agent is never handed A's row.
    """
    vis = record.get("visibility", TEAM)
    if vis == TEAM:
        return True
    return bool(user_id) and vis == f"user:{user_id}"


# --- write --------------------------------------------------------------------

def save_memory(
    text: str,
    *,
    kind: str = "fact",
    cue: Optional[Iterable[str]] = None,
    applies_to: Optional[str] = None,
    visibility: str = TEAM,
    author: str = "unknown",
    session_id: str = "",
    stated: bool = False,
) -> dict[str, Any]:
    """Write one memory. Returns the stored record.

    **When to save — the four-question sort, in order** (Session 4). Most lines
    in a session are none of these, and `drop` is the default:

      1. Does a tool or a store already own this? -> it is already stored. For a
         durable architectural decision that is `append_decision_record`, not
         this. Do not keep a second copy here.
      2. Must the agent behave differently on EVERY future run? -> `kind="rule"`.
      3. Did it learn something it would otherwise re-ask? -> `kind="fact"`,
         and give it a `cue`, or it will never come back.
      4. None of the above? -> **drop it.** "The user said hello" is not memory.

    Two trust levels (T5.6):
      `stated=True`  the user said "remember that ..." -> saved `accepted`.
      `stated=False` the agent inferred it -> saved `proposed`, and only
                     assembled into context once a SECOND independent
                     observation promotes it. Saving the same inferred text
                     twice is what promotes it, so a one-off guess never
                     silently becomes a standing instruction.

    `visibility` is `"team"` or `"user:<id>"`. Private is not the default:
    saying so must be deliberate.
    """
    records = _load()
    now = _now()
    cue_list = sorted({c.strip().lower() for c in (cue or []) if c and c.strip()})
    clean = text.strip()

    # Same text, same scope -> not a new memory. Either a duplicate (no-op) or
    # the second observation that promotes an inferred one.
    for existing in records:
        if existing.get("text", "").strip().lower() != clean.lower():
            continue
        if existing.get("visibility") != visibility:
            continue
        if existing.get("status") == "proposed":
            # Reaching here at all IS the second observation — the text was
            # already saved once. Either that, or the user has now stated it
            # outright. Both promote.
            existing["status"] = "accepted"
            existing["promoted_at"] = now
            existing["promoted_by"] = "stated" if stated else "second_observation"
        merged = sorted(set(existing.get("cue", [])) | set(cue_list))
        existing["cue"] = merged
        _save(records)
        return existing

    record = {
        "memory_id": f"m_{uuid.uuid4().hex[:10]}",
        "kind": kind,
        "visibility": visibility,
        "cue": cue_list,
        "applies_to": applies_to,
        "text": clean,
        # Carries its source, so the renderer can quote it rather than obey it.
        "source": {"author": author, "session_id": session_id, "quoted": True},
        "status": "accepted" if stated else "proposed",
        "created_at": now,
        "last_used_at": None,
        "use_count": 0,
    }
    records.append(record)
    _save(records)
    return record


def promote_memory(memory_id: str) -> Optional[dict[str, Any]]:
    """Promote a `proposed` memory to `accepted` on explicit confirmation."""
    records = _load()
    for record in records:
        if record.get("memory_id") == memory_id:
            record["status"] = "accepted"
            record["promoted_at"] = _now()
            record["promoted_by"] = "confirmed"
            _save(records)
            return record
    return None


# --- read ---------------------------------------------------------------------

def retrieve_memory(
    query: str = "",
    *,
    user_id: Optional[str] = None,
    applies_to: Optional[Iterable[str]] = None,
    kinds: Optional[Iterable[str]] = None,
    statuses: Iterable[str] = ("accepted",),
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Memory `user_id` may see, ranked by cue match then recency.

    Scoring is keyword + recency on purpose (no embeddings, CLAUDE.md §4):
      +3  a cue term appears in the query
      +2  `applies_to` is in the current impact set — the graph doing the routing
      +1  a query token appears in the text

    `statuses` defaults to `accepted` only: an inferred, unpromoted memory is
    not yet allowed to shape behaviour.

    When NOT to call: not for architectural decisions — those are in the overlay
    DB and come back through `retrieve_decisions`. This store holds what the
    agent learned in conversation.
    """
    records = _load()
    q_tokens = _tokens(query)
    impact = {a for a in (applies_to or []) if a}
    kind_set = set(kinds) if kinds else None
    status_set = set(statuses) if statuses else None

    scored: list[tuple[int, str, dict]] = []
    for record in records:
        if not _visible(record, user_id):
            continue
        if kind_set and record.get("kind") not in kind_set:
            continue
        if status_set and record.get("status") not in status_set:
            continue

        score = 0
        for term in record.get("cue") or []:
            if term in q_tokens or term in query.lower():
                score += 3
        if record.get("applies_to") and record["applies_to"] in impact:
            score += 2
        if q_tokens & _tokens(record.get("text", "")):
            score += 1

        # A rule with no cue and no binding is a standing instruction: it is
        # always a candidate, and the assembler decides whether to push it.
        if score == 0 and record.get("kind") == "rule" and not record.get("applies_to"):
            score = 1
        if score > 0:
            scored.append((score, record.get("created_at") or "", record))

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _, _, r in scored[:limit]]


def mark_used(memory_ids: Iterable[str]) -> None:
    """Record that these memories were actually assembled into a run.

    Cheap usage telemetry: a memory that never surfaces has a bad cue, which is
    a retrieval bug worth being able to see.
    """
    ids = set(memory_ids)
    if not ids:
        return
    records = _load()
    now = _now()
    for record in records:
        if record.get("memory_id") in ids:
            record["last_used_at"] = now
            record["use_count"] = int(record.get("use_count") or 0) + 1
    _save(records)


def all_memories(user_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Every memory visible to `user_id` — for demos, tests, and the judge."""
    return [r for r in _load() if _visible(r, user_id)]
