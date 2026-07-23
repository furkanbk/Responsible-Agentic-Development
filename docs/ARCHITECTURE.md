# ARCHITECTURE.md

> **Skeleton — HW1.** This file is the durable-knowledge layer for the repo. Every merged PR
> that adds or changes a component updates it. Sections marked `<!-- OWNER: ... -->` are
> filled in by that owner as their work lands. Keep entries terse; this is read instead of
> the code.
>
> Last updated: _(date / PR #)_

---

## 1. Purpose

RADF keeps a persistent, machine-readable map of a codebase (components, dependencies, and
past decisions) so that each new agent session starts from accumulated knowledge instead of
re-deriving structure by grepping.

**HW1 scope:** one agent, one tool registry, one JSON-backed knowledge graph. No
orchestrator, no multi-agent pipeline, no framework.

---

## 2. Current state (HW1)

```
user query
    │
    ▼
run_agent()  ── observe → reason → act → verify ──┐
    │  guards: max_steps · stall detection · done-signal
    │  gate:   irreversible tools require explicit y/n
    │  error:  invalid / implausible tool output → error branch
    ▼                                             │
tool registry ────────────────────────────────────┘
    │
    ▼
store/knowledge_graph.json   (nodes · edges · decisions)
```

---

## 3. Components

<!-- One entry per module. Add yours when your PR lands. -->

### `agentlib/core.py` — LLM runtime wrapper
<!-- OWNER: Berat -->
- **Owns:** the single call path to the OpenCode Zen API (OpenAI-compatible, Responses
  endpoint, base URL `https://opencode.ai/zen/v1`). Key from `.env` (`OPENCODE_API_KEY`),
  never hard-coded.
- **Exposes:** `call(...) -> Result`, `CHEAP`, `STRONG`, `MODELS`, `estimate_cost(...)`, `show(...)`
- **`Result` carries:** `.text`, `.tool_calls`, `.output_items`, `.status`, `.stop_reason`,
  `.truncated`, `.usage`
- **Contract notes:** `truncated=True` means the call hit the output-token cap — *returned
  text is not a finished result*. Callers must branch on it.
- **Cost accounting:** input tokens already include cached tokens (cheaper rate); output
  tokens already include reasoning tokens (normal output rate).
- **Depends on:** `openai`, `python-dotenv`
- **Status:** _TBD_

### `agentlib/schemas.py` — schema derivation
<!-- OWNER: Berat -->
- **Owns:** `schema_for(fn)` — derives a tool schema from signature + annotations + docstring.
- **Contract notes:** enums and other narrowing are applied by the tool author on top of the
  derived schema (see Part B, B1).
- **Status:** _TBD_

### `agentlib/guards.py` — mechanical guardrails
<!-- OWNER: Berat -->
- **Owns:** `validate_args(schema, args)`, output/truncation validation, stall detection,
  approval-gate policy (`GATED` set).
- **Contract notes:** every guard routes its failure to its own branch; nothing returns a
  failure as if it were valid data.
- **Status:** _TBD_

### `agentlib/loop.py` — the agent loop
<!-- OWNER: Berat -->
- **Owns:** `run_agent(user_msg, schemas, registry, approve, ...)`
- **Stopping conditions:** `answered` · `max_steps` · `stalled` · `declined`
- **Returns:** `{"answer", "steps", "trace", "stopped"}`
- **Status:** _TBD_

### `tools/repo_scan.py` — repository → graph
<!-- OWNER: Alejandro -->
- **Owns:** `scan_repository_structure(...)`
- **Writes to:** `store/knowledge_graph.json` (nodes + edges)
- **Contract:** _fill in on implementation — final signature, return shape, node/edge schema_
- **Status:** _stub_

### `tools/graph_query.py` — graph → answers
<!-- OWNER: Alejandro -->
- **Owns:** `query_component_graph(...)`
- **Reads:** `store/knowledge_graph.json`. Read-only, reversible, ungated.
- **Contract:** _fill in on implementation_
- **Status:** _stub_

### `tools/decisions.py` — decision log
<!-- OWNER: Dias -->
- **Owns:** `append_decision_record(...)`, `verify_graph_integrity(...)`
- **Contract:** append-only, reversible, ungated. `verify_graph_integrity` returns a
  structured `{"error": ...}` on a corrupt/implausible graph rather than raising or
  returning it as normal data — this is the loop's error branch (Part B, B2).
- **Status:** _stub_

### `tools/graph_write.py` — destructive graph edits
<!-- OWNER: Dias -->
- **Owns:** `prune_graph_node(...)`
- **Contract:** **irreversible** — listed in `GATED`, requires explicit human approval.
- **Status:** _stub_

### `main.py` — CLI entry point
<!-- OWNER: Berat -->
- **Owns:** argument parsing, `.env` load, registry assembly, `input()`-based approval callback.
- **Status:** _TBD_

---

## 4. Data contracts

### `store/knowledge_graph.json`
<!-- OWNER: Alejandro defines; Dias consumes -->
```jsonc
{
  "nodes":     [ { "id": "", "path": "", "kind": "", "symbols": [] } ],
  "edges":     [ { "from": "", "to": "", "relation": "" } ],
  "decisions": [ { "component": "", "decision": "", "rationale": "", "status": "", "ts": "" } ],
  "meta":      { "scanned_at": "", "root": "" }
}
```
_Authoritative version of this schema is filled in by the `repo_scan` PR._

**Invariant — structure and decisions are separate layers.** `nodes` and `edges` are *derived*: any
scan may regenerate them wholesale, and nothing outside the scanner may hand-edit them. `decisions`
is *authored*: it is the durable knowledge the project exists to accumulate, and no scan may
overwrite it. In HW1 both live in one file for convenience, but they are joined by `symbol_uid`,
never merged — a decision references a node, it is never stored *inside* one. This keeps the
decision layer portable when the structural half is later replaced by an external indexer (§6).

A decision whose `symbol_uid` no longer resolves to a node is **orphaned**, not deleted: the
component moved or was removed, so the decision may be stale and should be surfaced for review.
`verify_graph_integrity` reports orphans as a structured error rather than dropping them.

---

## 5. Decision log

> Append one entry per non-obvious choice. Newest last. Do not delete entries — supersede them.

| # | Date | Component | Decision | Rejected alternative | Why |
|---|------|-----------|----------|----------------------|-----|
| 1 | | repo-wide | Knowledge graph stored as a single JSON file | SQLite, graph DB | HW1 forbids extra deps; JSON is inspectable and diffable in PRs |
| 2 | | agentlib | Raw Python loop, no framework | LangGraph | HW1 constraint; framework refactor is Session 9 |
| 3 | | tools | `prune_graph_node` is the only gated tool | gating all writes | Irreversibility decides the gate; append/scan are recoverable |
| 4 | | indexing | GitNexus selected as the future structural indexer (post-HW1) | CodeGraph; continuing with hand-rolled `ast` scanning long-term | GitNexus stores the graph in an embedded graph DB (LadybugDB) and exposes a raw `cypher` tool plus a published schema resource, so custom entities and traversals are supported through the public API. CodeGraph is SQLite/FTS5 behind a single `codegraph_explore` tool — extending it means reaching into internals its own file-watcher continuously rewrites. |
| 5 | | indexing | Decisions live in a **separate overlay**, joined to structure by `symbol_uid` — never written into indexer-owned nodes | enriching the indexer's nodes in place with decision metadata | Both candidate indexers re-index aggressively (CodeGraph on every file event, GitNexus on `analyze`). Anything injected into their extraction output is overwritten on the next sync, and couples our core contribution to their internal schema. The overlay makes re-indexing free and makes a stale reference visible as an orphan rather than a silent loss. |
| 6 | | licensing | GitNexus is PolyForm Noncommercial; CodeGraph is MIT | — | Acceptable for coursework, recorded because it forecloses commercial use of this repo downstream. Revisit if the project outlives the course. |
| 7 | | | | | |

---

## 6. Known gaps / deferred to later homeworks

- Orchestrator + Architecture/Discussion agent split (multi-agent, Session 5)
- ELI5 agent with Mermaid output
- Retrieval over the graph (Session 10) — currently exact lookup only
- Evaluation harness: coupling drift, decision consistency, rework rate, context cost (Session 11)

### 6.1 Deferred: replace the structural half with GitNexus

**Not in HW1 scope.** HW1 deliberately hand-rolls structural extraction (`ast`-based
`scan_repository_structure`) so the team understands what an indexer does before delegating it.
The swap is a natural fit for the Session 9 framework-refactor deliverable — "refactor one
first-half component and compare what was easier, what was hidden."

**Design (recorded now, built later).** Only the *structural* half is replaced. The decision
overlay stays ours and unchanged:

```
decision overlay          ← ours. authored, durable, never regenerated
  decision_id · symbol_uid · component · decision · rationale ·
  rejected_alternative · status · session_id · ts
        │
        │  joined on symbol_uid
        ▼
GitNexus graph            ← theirs. derived, regenerated freely, never hand-edited
  symbols · edges · clusters · processes
```

- The Architecture Agent performs two lookups per query: structure from GitNexus
  (`context` / `impact` / `trace`), prior decisions from the overlay.
- GitNexus emits stable symbol uids (e.g. `Function:src/embed.py:get_embeddings`); the overlay
  keys on those. Migration from the HW1 JSON graph is a uid remap, not a rewrite.
- If decisions are ever wanted *in* the graph for traversal, materialize them as `:Decision`
  nodes via the `cypher` tool as a **derived, write-only step after each analyze**. The overlay
  remains the source of truth; the materialized nodes are disposable.
- Integration is via MCP or the CLI, not a library import — GitNexus is TypeScript, our agents
  are Python, and its CLI mirrors the MCP tools with JSON output.

**Open at migration time:** where the overlay lives once it outgrows JSON (SQLite is the likely
answer, Session 4); how orphaned uids are triaged (auto-flag vs. Discussion Agent review); and
whether `verify_graph_integrity` moves to checking overlay-vs-GitNexus consistency instead of
internal graph consistency.
