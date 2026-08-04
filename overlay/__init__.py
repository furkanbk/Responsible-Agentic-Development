"""overlay — the authored, durable half of the knowledge graph.

Owner: Berat Furkan Kocak (HW2, Phase 4).

Two layers, joined on `symbol_uid`, never merged (CLAUDE.md §6):

    structure   store/knowledge_graph.json  ->  GitNexus/LadybugDB later
                derived; any scan regenerates it wholesale

    overlay     store/radf.db (this package)
                authored; no scan or re-index may touch it

The structural half is a deliberate stand-in and is NOT built up further in HW2
(ARCHITECTURE.md §6.1) — investing in it would turn the promised uid remap back
into a migration. What is durable lives here.
"""

from __future__ import annotations

from .uid import resolve_uid, uid_matches_component
from .db import (
    connect,
    init_db,
    db_path,
    insert_decision,
    query_decisions,
    decisions_across_scopes,
    all_decision_uids,
    import_legacy_decisions,
    upsert_node_summary,
    query_node_summaries,
    all_summary_uids,
    stale_summaries,
    MODULE_CARD,
    start_run,
    finish_run,
    scratch_write,
    scratch_read,
    scratch_dump,
    record_silence,
    query_silences,
    count_silences,
)
from .memory import (
    save_memory,
    retrieve_memory,
    promote_memory,
    mark_used,
    all_memories,
    memory_path,
    MemoryStoreCorrupt,
)

__all__ = [
    "resolve_uid",
    "uid_matches_component",
    "connect",
    "init_db",
    "db_path",
    "insert_decision",
    "query_decisions",
    "decisions_across_scopes",
    "all_decision_uids",
    "import_legacy_decisions",
    "upsert_node_summary",
    "query_node_summaries",
    "all_summary_uids",
    "stale_summaries",
    "MODULE_CARD",
    "start_run",
    "finish_run",
    "scratch_write",
    "scratch_read",
    "scratch_dump",
    "record_silence",
    "query_silences",
    "count_silences",
    "save_memory",
    "retrieve_memory",
    "promote_memory",
    "mark_used",
    "all_memories",
    "memory_path",
    "MemoryStoreCorrupt",
]
