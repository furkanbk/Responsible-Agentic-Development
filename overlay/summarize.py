"""overlay.summarize — bootstrap the authored node summaries.

Owner: Berat Furkan Kocak (HW5, T14.7).

    python -m overlay.summarize            # fill gaps and refresh stale cards
    python -m overlay.summarize --stale    # report what is stale; write nothing
    python -m overlay.summarize --force    # re-summarise everything

Walks the derived graph, reads each node's source, and asks `CHEAP` for one
module card plus one card per symbol the scanner found. Those cards are the
corpus `search_corpus` searches, which is what lets a request phrased the way
people phrase them resolve to the components it touches.

**Idempotent by `content_sha`.** A node whose file has not changed since its
cards were written is skipped entirely — no call, no cost. That is also the
staleness signal: `content_sha` is compared by *code*, so nothing depends on an
agent remembering to update a summary after an edit (#34, #38). An operating
rule saying "update the summary when you change a file" is a rule the model can
silently skip, and detecting the skip requires this comparison anyway.

The summaries are **authored** and live in the overlay (#57). They are the
expensive artefact here — one model call each — and writing them into
`knowledge_graph.json` would mean the next `scan_repository_structure` deletes
them with no error at all (#16).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

from agentlib.core import CHEAP, call

from .db import MODULE_CARD, connect, query_node_summaries, upsert_node_summary
from .uid import resolve_uid

_REPO_ROOT = Path(__file__).resolve().parent.parent

# How much of a file the summariser sees. Enough for the module's shape and its
# public surface; a cheap model given 60k characters summarises the last third.
MAX_SOURCE_CHARS = 12_000

AUTHOR = "summarizer"

_INSTRUCTION = """You describe one Python module of a codebase so that a
developer searching in plain language can find it.

Return ONLY a JSON object:
{
  "module": {"summary": "...", "responsibility": "..."},
  "symbols": [{"name": "...", "signature": "...", "summary": "...",
               "responsibility": "..."}]
}

Rules:
- "summary" is one sentence: what it IS.
- "responsibility" is one sentence: what it OWNS, or when someone would need to
  change it. Write it so it matches how a person would describe the TASK, not
  the code — "stops the agent repeating the same tool call" beats "compares two
  strings".
- Cover only the symbols listed in SYMBOLS. Do not invent any.
- No markdown, no code fences, no prose outside the JSON.
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _brace_slice(text: str) -> str:
    """Extract the first balanced {...} object.

    A depth scan, not `rfind("}")` — the trailing-brace defect filed against
    `agents/planner.py::_brace_slice` made roughly one cheap-model reply in four
    unparseable, and this module makes one call per node.
    """
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _parse(text: str) -> Optional[dict]:
    blob = _brace_slice(text or "")
    if not blob:
        return None
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _graph_path() -> Path:
    import os
    override = os.environ.get("RADF_GRAPH_PATH")
    return Path(override) if override else _REPO_ROOT / "store" / "knowledge_graph.json"


def _load_nodes() -> list[dict]:
    path = _graph_path()
    if not path.exists():
        return []
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [n for n in graph.get("nodes", []) if isinstance(n, dict)]


def summarize_node(node: dict, source: str, *, model: str = CHEAP) -> Optional[dict]:
    """One model call -> the module card plus its symbol cards."""
    symbols = [s for s in (node.get("symbols") or []) if isinstance(s, str)]
    prompt = (
        f"MODULE: {node.get('id')}\n"
        f"PATH: {node.get('path')}\n"
        f"SYMBOLS: {', '.join(symbols) if symbols else '(none)'}\n\n"
        f"SOURCE:\n{source[:MAX_SOURCE_CHARS]}"
    )
    result = call(prompt, system=_INSTRUCTION, model=model, max_output_tokens=2000)
    return _parse(result.text or "")


def run(
    *,
    force: bool = False,
    stale_only: bool = False,
    model: str = CHEAP,
    limit: Optional[int] = None,
    verbose: bool = True,
) -> dict:
    """Fill in missing and stale summaries. Returns a small report."""
    nodes = _load_nodes()
    if not nodes:
        return {"nodes": 0, "skipped": 0, "written": 0, "failed": 0,
               "error": "no graph — run scan_repository_structure first"}

    conn = connect()
    try:
        known = {
            (row["symbol_uid"], row["symbol"]): row["content_sha"]
            for row in query_node_summaries(conn)
        }

        report = {"nodes": 0, "skipped": 0, "written": 0, "failed": 0, "stale": []}
        summarized = 0
        for node in nodes:
            path = _REPO_ROOT / str(node.get("path") or "")
            if not path.is_file() or path.suffix != ".py":
                continue
            uid = resolve_uid(str(node.get("id") or node.get("path") or ""))
            if not uid:
                continue

            report["nodes"] += 1
            source = path.read_text(encoding="utf-8", errors="replace")
            sha = _sha(source)

            current = known.get((uid, MODULE_CARD))
            if current == sha and not force:
                report["skipped"] += 1
                continue
            if current is not None and current != sha:
                report["stale"].append(uid)
            if stale_only:
                continue
            if limit is not None and summarized >= limit:
                continue
            summarized += 1

            parsed = summarize_node(node, source, model=model)
            if not parsed or not isinstance(parsed.get("module"), dict):
                report["failed"] += 1
                if verbose:
                    print(f"  ! {uid}: unparseable reply", file=sys.stderr)
                continue

            module = parsed["module"]
            upsert_node_summary(
                conn,
                component=uid,
                symbol=None,
                summary=str(module.get("summary", "")).strip() or uid,
                responsibility=str(module.get("responsibility", "")).strip(),
                content_sha=sha,
                author_id=AUTHOR,
            )
            written = 1

            declared = {s for s in (node.get("symbols") or []) if isinstance(s, str)}
            for entry in parsed.get("symbols") or []:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name", "")).strip()
                # Only symbols the SCANNER found. The model is asked not to
                # invent any; this is the code-side check that it did not, and
                # keeps the corpus joined to real structure (#38).
                if not name or name not in declared:
                    continue
                upsert_node_summary(
                    conn,
                    component=uid,
                    symbol=name,
                    summary=str(entry.get("summary", "")).strip() or name,
                    responsibility=str(entry.get("responsibility", "")).strip(),
                    signature=str(entry.get("signature", "")).strip() or None,
                    content_sha=sha,
                    author_id=AUTHOR,
                )
                written += 1

            report["written"] += written
            if verbose:
                print(f"  + {uid}: {written} card(s)")
        return report
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise graph nodes into the overlay.")
    parser.add_argument("--force", action="store_true", help="re-summarise even if unchanged")
    parser.add_argument("--stale", action="store_true", help="report stale cards, write nothing")
    parser.add_argument("--limit", type=int, default=None, help="stop after N nodes (smoke runs)")
    args = parser.parse_args(argv)

    report = run(force=args.force, stale_only=args.stale, limit=args.limit)
    if report.get("error"):
        print(report["error"], file=sys.stderr)
        return 1
    print(f"nodes={report['nodes']} written={report['written']}"
          f" skipped={report['skipped']} failed={report['failed']}")
    if report.get("stale"):
        print(f"stale: {len(report['stale'])} -> {', '.join(report['stale'][:10])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
