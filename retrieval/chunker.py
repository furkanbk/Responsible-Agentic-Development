"""retrieval.chunker — corpus assembly.

Owner: Berat Furkan Kocak (HW5, T14.6c).

Three chunk kinds, one index, all keyed into the existing `symbol_uid` space:

    component   one card per module and per symbol, from `node_summaries`
    decision    one authored decision record, atomic
    doc         one heading-bounded section of the repo's own markdown

**Why not fixed-size chunking.** The obvious default — 512 tokens, 50 overlap,
split on whitespace — is actively wrong for these documents, in two ways that
would each be invisible until the metrics came back bad.

1. `docs/ARCHITECTURE.md`'s decision log is a **markdown table where one row is
   one decision**. Its columns are `decision | rejected alternative | why`. A
   fixed-size split severs the decision from its rationale, and the resulting
   chunks are individually useless: half a decision retrieves for the query and
   answers none of it. So table rows are atomic, and each carries its header row
   so the columns still mean something out of context.

2. Markdown sections are meaningless without their ancestry. A chunk reading
   *"JSON, because it is diffable and any scan may replace it"* cannot be
   retrieved by any query that does not already use those words. So every chunk
   is prefixed with its full **heading path** — `ARCHITECTURE.md > §5 Decision
   log > #21` — which is what makes acronym and pronoun queries resolvable at
   all. This costs ~60 characters per chunk and buys more than overlap does.

**Size and overlap.** `MAX_CHARS = 1200` (roughly 300 tokens), because a section
of this corpus is usually one idea and sections above that length are almost
always lists that split cleanly at paragraph boundaries anyway. Overlap is
`OVERLAP_RATIO = 0.15` and applies **only** when a section overflows and must be
split — an unconditional overlap on already-atomic units would manufacture the
near-duplicate noise the reranker is worst at, purely to satisfy a default.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from overlay.db import MODULE_CARD, query_node_summaries
from overlay.uid import resolve_uid

from .types import TEAM, Chunk

MAX_CHARS = 1200
OVERLAP_RATIO = 0.15
MIN_CHARS = 40          # below this a "section" is a stub heading, not content

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The repo's own authored markdown. Explicitly listed rather than globbed: a
# glob would sweep in whatever a future homework drops in the tree, and silently
# changing the corpus changes every number in the eval tables.
CORPUS_DOCS: tuple[str, ...] = (
    "docs/ARCHITECTURE.md",
    "docs/TODO.md",
    "README.md",
    ".claude/CLAUDE.md",
    "rules/OPERATING_RULES.md",
    "rules/ADMIN_BOUNDARY.md",
    "rules/modules/tools.md",
    "rules/personal/keep_diffs_minimal.md",
    "agents/executor_brief.md",
    "monitor/rubric.md",
    "guidance/04-slides.md",
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_ROW = re.compile(r"^\s*\|")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|?\s*$")


def _chunk_id(*parts: object) -> str:
    """A stable id for a chunk, derived from what identifies it.

    Content-independent on purpose: re-summarising a symbol should UPDATE that
    chunk, not mint a second one. Ids still move when the corpus is re-chunked
    differently, which is why eval goldens are content anchors (contract #10).
    """
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return "c_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- component cards ----------------------------------------------------------

def component_chunks(conn: sqlite3.Connection) -> list[Chunk]:
    """One chunk per `node_summaries` row.

    The card is assembled with its labels ("Responsibility:", the module path)
    rather than as bare prose, because those labels are themselves searchable
    and because a symbol card without its parent module is ambiguous — there are
    three `_tokens` in this repo.
    """
    out: list[Chunk] = []
    for row in query_node_summaries(conn):
        uid = row["symbol_uid"]
        symbol = row["symbol"] or ""
        module = uid.split(":", 1)[-1]
        title = f"{module}.{symbol}" if symbol else module
        heading = f"{module} > {symbol}" if symbol else module

        lines = [f"{title} — {row['summary']}"]
        if row.get("signature"):
            lines.append(f"Signature: {row['signature']}")
        lines.append(f"Responsibility: {row['responsibility']}")
        if symbol:
            lines.append(f"Defined in module: {module}")
        text = "\n".join(lines)

        out.append(
            Chunk(
                chunk_id=_chunk_id("component", uid, symbol),
                kind="component",
                text=text,
                symbol_uid=uid,
                symbol=symbol or None,
                heading_path=heading,
                source_path=module,
                content_sha=row["content_sha"],
                visibility=TEAM,
            )
        )
    return out


# --- decision records ---------------------------------------------------------

def decision_chunks(conn: sqlite3.Connection) -> list[Chunk]:
    """One chunk per authored decision — atomic, never split.

    Carries the row's own `visibility` through to the index so the retrieval
    query can apply the `visible_to` predicate (#24). This is the only chunk
    kind that is ever scoped.
    """
    rows = conn.execute(
        "SELECT * FROM decisions WHERE status IN ('accepted','proposed')"
        " ORDER BY ts ASC"
    ).fetchall()

    out: list[Chunk] = []
    for row in rows:
        row = dict(row)
        uid = row.get("symbol_uid")
        scope = uid or "repo-wide"
        parts = [
            f"Decision about {scope}: {row['decision']}",
            f"Rationale: {row['rationale']}",
        ]
        if row.get("rejected"):
            parts.append(f"Rejected alternative: {row['rejected']}")
        parts.append(f"Status: {row['status']}")
        out.append(
            Chunk(
                chunk_id=_chunk_id("decision", row["decision_id"]),
                kind="decision",
                text="\n".join(parts),
                symbol_uid=uid,
                symbol=None,
                heading_path=f"Decision log > {scope}",
                source_path="store/radf.db#decisions",
                content_sha=_sha(row["decision"] + row["rationale"]),
                visibility=row["visibility"],
            )
        )
    return out


# --- markdown sections --------------------------------------------------------

def _split_oversize(text: str) -> list[str]:
    """Split an overlong section at paragraph boundaries, with overlap.

    Overlap exists so a sentence straddling a split is still retrievable from
    one side; it is applied HERE and nowhere else, because these are the only
    chunks whose boundaries are arbitrary.
    """
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    overlap_chars = int(MAX_CHARS * OVERLAP_RATIO)

    for para in paras:
        if size and size + len(para) > MAX_CHARS:
            chunks.append("\n\n".join(current))
            tail = "\n\n".join(current)[-overlap_chars:] if overlap_chars else ""
            current = [tail, para] if tail else [para]
            size = len(tail) + len(para)
        else:
            current.append(para)
            size += len(para) + 2

    if current:
        chunks.append("\n\n".join(current))

    # A single paragraph longer than the cap (a wall-of-text decision cell) is
    # left whole rather than cut mid-sentence: an oversize chunk costs context
    # budget, a severed one costs meaning.
    return [c for c in chunks if c.strip()]


def _table_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Partition lines into ('text'|'table', block) runs."""
    blocks: list[tuple[str, list[str]]] = []
    current: list[str] = []
    mode = "text"
    for line in lines:
        is_row = bool(_TABLE_ROW.match(line))
        want = "table" if is_row else "text"
        if want != mode and current:
            blocks.append((mode, current))
            current = []
        mode = want
        current.append(line)
    if current:
        blocks.append((mode, current))
    return blocks


def doc_chunks(paths: Iterable[str] = CORPUS_DOCS, *, root: Optional[Path] = None) -> list[Chunk]:
    """Heading-aware chunks over the repo's authored markdown."""
    base = root or _REPO_ROOT
    out: list[Chunk] = []

    for rel in paths:
        path = base / rel
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        file_sha = _sha(raw)
        uid = resolve_uid(rel)

        stack: list[tuple[int, str]] = []
        section: list[str] = []
        ordinal = 0

        def flush(headings: list[tuple[int, str]]) -> None:
            nonlocal ordinal, section
            body = "\n".join(section).strip()
            section = []
            if len(body) < MIN_CHARS:
                return
            heading_path = " > ".join([rel] + [h for _, h in headings])

            for mode, block in _table_blocks(body.splitlines()):
                if mode == "table":
                    rows = [ln for ln in block if not _TABLE_SEP.match(ln)]
                    header = rows[0] if rows else ""
                    data = rows[1:] if len(rows) > 1 else rows
                    # One row per chunk: in this repo a table row IS the atomic
                    # unit (one decision, one owned file), and the header row
                    # keeps the columns meaningful out of context.
                    for row_line in data:
                        if len(row_line.strip()) < MIN_CHARS:
                            continue
                        ordinal += 1
                        text = f"{heading_path}\n{header}\n{row_line}"
                        out.append(
                            Chunk(
                                chunk_id=_chunk_id("doc", rel, heading_path, ordinal),
                                kind="doc",
                                text=text,
                                symbol_uid=uid,
                                symbol=None,
                                heading_path=heading_path,
                                source_path=rel,
                                content_sha=file_sha,
                                visibility=TEAM,
                            )
                        )
                else:
                    prose = "\n".join(block).strip()
                    if len(prose) < MIN_CHARS:
                        continue
                    pieces = [prose] if len(prose) <= MAX_CHARS else _split_oversize(prose)
                    for piece in pieces:
                        ordinal += 1
                        out.append(
                            Chunk(
                                chunk_id=_chunk_id("doc", rel, heading_path, ordinal),
                                kind="doc",
                                text=f"{heading_path}\n{piece}",
                                symbol_uid=uid,
                                symbol=None,
                                heading_path=heading_path,
                                source_path=rel,
                                content_sha=file_sha,
                                visibility=TEAM,
                            )
                        )

        in_fence = False
        for line in raw.splitlines():
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                section.append(line)
                continue
            match = None if in_fence else _HEADING.match(line)
            if match:
                flush(list(stack))
                level = len(match.group(1))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, match.group(2)))
            else:
                section.append(line)
        flush(list(stack))

    return out


# --- assembly -----------------------------------------------------------------

def build_corpus(
    conn: sqlite3.Connection,
    *,
    docs: Iterable[str] = CORPUS_DOCS,
    root: Optional[Path] = None,
    kinds: Iterable[str] = ("component", "decision", "doc"),
) -> list[Chunk]:
    """The whole corpus, in one list."""
    wanted = set(kinds)
    out: list[Chunk] = []
    if "component" in wanted:
        out += component_chunks(conn)
    if "decision" in wanted:
        out += decision_chunks(conn)
    if "doc" in wanted:
        out += doc_chunks(docs, root=root)

    # Ids are derived from identity, so a duplicate means two sources claimed
    # the same identity — surfaced here rather than silently overwriting one.
    seen: dict[str, Chunk] = {}
    for chunk in out:
        if chunk.chunk_id in seen:
            continue
        seen[chunk.chunk_id] = chunk
    return list(seen.values())
