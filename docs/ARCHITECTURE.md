# ARCHITECTURE.md

> This file is the durable-knowledge layer for the repo. Every merged PR that adds or changes a
> component updates it. Sections marked `<!-- OWNER: ... -->` are filled in by that owner as
> their work lands. Keep entries terse; this is read instead of the code.
>
> Last updated: 2026-07-26 — HW2 state layers, overlay, and the two-agent pipeline (Berat);
> planner + gated `apply_change` implemented, branch `hw2/alejandro/planner-and-write` (Alejandro)
>
> **2026-08-02 — HW4 begins:** framework refactor onto LangGraph/LangChain (decisions #49-#53),
> branch `hw4/berat/langgraph-foundation` (Berat). HW1-HW3 sections below are historical record.

---

## 1. Purpose

RADF keeps a persistent, machine-readable map of a codebase (components, dependencies, and
past decisions) so that each new agent session starts from accumulated knowledge instead of
re-deriving structure by grepping.

**HW1 scope (closed):** one agent, one tool registry, one JSON-backed knowledge graph. No
orchestrator, no multi-agent pipeline, no framework.

**HW2 scope (current):** state split across the stores that fit it, memory scoped per user with
a shared team layer, a planner and an executor coordinating through a structured envelope, and a
monitor that grades runs from outside the loop. Still no framework (CLAUDE.md §4).

The team scenario is what HW2 is really about: a module has conventions the *team* agreed on
**and** per-engineer working preferences. Both must reach the agent, they must not be confused
with each other, and one engineer's private preference must never surface in another's run.

---

## 2. Current state (HW5)

HW3 puts a surface in front of the HW2 pipeline. Everything below the dashed line is
unchanged; what is new is that something other than a person at a terminal can start a run,
and that a run can correctly end in saying nothing.

**HW4** replaced the hand-rolled loop internals with LangGraph and the tool interface with
LangChain `StructuredTool`s, without changing `run_agent`'s signature or return shape
(decisions #49-#55) — so the diagram below is unchanged by it.

**HW5** adds one tool to the loop and one input to the planner. `search_corpus` searches
authored summaries of every module and symbol, so a vague request resolves to the components it
touches — which is what the planner's seed step needed and never had. Retrieval is a tool the
model chooses (decision #60), so nothing in the flow below runs unless the model asks for it.

```
  TELEGRAM ──poll──┐                                  ┌──> reply
  GITHUB webhook ──┼─> InboundEvent ─> identity ─> QUEUE ─> [ONE worker] ─> silence? ─┤
  HEARTBEAT ───────┘   (one shape)     (ambient)      │         │                     └──> record
                                                      │         │                          (say nothing)
                       admission policy, per path ────┘         │
                         webhook   -> coalesce                  ├──> question  -> run_agent (read-only tools)
                         heartbeat -> drop duplicate            └──> /change   -> ORCHESTRATOR (below)
                         human     -> queue, reject while gated                        │
                                                                                       │
 ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
                      rules/*.md ──┐ push (every run)
                                   │
change request ──> ORCHESTRATOR (plain Python — branches on envelope fields, no model)
                        │
                        ├─1─> PLANNER ──> query_component_graph  (structure: what breaks)
                        │        │        retrieve_decisions     (overlay:   why it is so)
                        │        └──> AgentResult{status, result, needs_approval}
                        │                     │
                        │              status == ok ?  ──no──> stop: needs_input / blocked / failed
                        │                     │ yes
                        │            run_scratch[plan]          <- append-only, reads logged
                        │                     │
                        └─2─> EXECUTOR <──────┘  narrow toolset + executor_brief.md
                                 │              impact_scope() bounds what it may write
                                 └──> apply_change  ── GATED ──> human y/n
                                                │
    ┌───────────────────────────────────────────┘
    ▼
store/runs/runs.jsonl  ──(own schedule, separate agent, read-only)──> MONITOR / judge
```

In HW3 the gate's `y/n` arrives as another channel message rather than on stdin (decision
#41), and the monitor's "own schedule" becomes an actual clock (T11.1, Dias).

**Five stores, five jobs:**

```
store/knowledge_graph.json   derived     nodes · edges          any scan regenerates it
store/radf.db                authored    decisions · runs ·     no scan may touch it
                                         run_scratch · silences ·
                                         node_summaries (HW5)
store/memory.json            authored    free-form facts+rules  cue-retrieved
rules/*.md                   authored    operating rules        edited by hand, pushed every run
Postgres + pgvector (HW5)    derived     chunks · embeddings    droppable; rebuilt by reindex
```

The first is a **deliberate stand-in** for GitNexus and is not being built out (§6.1,
decision #21). The last is derived too, and is the only store that is not a file — it exists
because the retrieval index needs vector similarity and metadata filtering in one query
(decision #56). The three authored stores in the middle are ours and are the durable half; the
overlay is explicitly **not** migrating into Postgres now that one is running (decision #62).

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
  text is not a finished result*. Callers must branch on it. `output_items` is **replay-safe**:
  `reasoning` items are dropped and the server `id` is stripped so the list can be fed straight
  back as next-turn input without a provider 400 (decision #20); `raw` keeps the untouched response.
- **Cost accounting:** input tokens already include cached tokens (cheaper rate); output
  tokens already include reasoning tokens (normal output rate).
- **Depends on:** `openai`, `python-dotenv`
- **Note:** the notebooks import `from agentlib.tools import ...`; this repo standardizes on
  `agentlib.core` (same surface). See decision #7.
- **Models:** `CHEAP = gpt-5.4-nano`, `STRONG = gpt-5.5`, both confirmed live against the Zen
  `/models` listing and priced in `MODELS`. Overridable via `OPENCODE_CHEAP_MODEL` /
  `OPENCODE_STRONG_MODEL`. `call()` accepts `prompt | messages`, `system`, `model`, `tools`,
  `max_output_tokens`. `Result` fields as listed above are implemented.
- **Known provider gap:** every `gemini-*` id is listed by Zen but 400s on both
  `/responses` and `/chat/completions` (`Invalid JSON request body: Missing key at
  ["contents"]` — Zen forwards the OpenAI-shaped body to Google untranslated). Do not
  select a Gemini model. See decision #19.
- **Status:** **done** (Phase 0)

### `agentlib/schemas.py` — schema derivation
<!-- OWNER: Berat -->
- **Owns:** `schema_for(fn)` — derives a tool schema from signature + annotations + docstring.
- **Contract notes:** a `Literal[...]` param annotation derives into a JSON-Schema `enum`
  (decision #8); string annotations (PEP 563) are resolved via `get_type_hints`. All other
  narrowing (numeric bounds, extra enums, "when NOT to call" prose) is applied by the tool
  author on top of the derived schema (see Part B, B1).
- **Status:** **done** (Phase 0)

### `agentlib/guards.py` — mechanical guardrails
<!-- OWNER: Berat -->
- **Owns:** `validate_args(schema, args)`, `check_output(result)` (truncation),
  `is_error_result(out)` (error-branch detection), `detect_stall(signatures)` +
  `call_signature(...)`, `GATED` set + `requires_approval(name)`.
- **Contract notes:** every guard routes its failure to its own branch; nothing returns a
  failure as if it were valid data. `GATED = {"prune_graph_node"}` — only irreversible.
- **Status:** **done** (Phase 0)

### `agentlib/loop.py` — the agent loop
<!-- OWNER: Berat -->
- **Owns:** `run_agent(user_msg, schemas, registry, approve, ...)`
- **Stopping conditions:** `answered` · `max_steps` · `stalled` · `declined` · `truncated`.
  A gate decline feeds a `declined` result back so the model can react (Part B, B4); the
  loop terminates with `stopped="declined"` only if the model then re-issues the same blocked
  call (decision #10). `truncated` guards the model's own output (decision #9).
- **Branches (own-branch, never dressed as data):** invalid-args, gate/decline, tool-returned
  structured error (B2). Tool output re-enters context wrapped as `{"result": ...}` data.
- **Returns:** `{"answer", "steps", "trace", "stopped"}`; `trace` events carry a `branch` tag.
- **HW4 note (decision #49):** internals are now a compiled LangGraph graph
  (`agentlib/graph.py`); the signature and return shape above are **frozen unchanged** so
  `agents/executor.py`, `agents/admin.py`, `service.py`, and `main.py` needed zero edits.
- **Status:** **done** (Phase 0); internals refactored onto LangGraph (Phase 12)

### `agentlib/graph_state.py` — LangGraph state schema
<!-- OWNER: Berat -->
- **Owns:** `AgentState`, a `TypedDict` — `messages` (LangChain `BaseMessage` list, reduced
  with `add_messages`), `trace`, `signatures`, `declined_signatures`, `step`, `stopped`, `answer`.
  Same fields `loop.py` already tracked as local variables (decision #51) — not an ad-hoc dict.
- **Depends on:** `langgraph`, `langchain_core`
- **Status:** **done** (Phase 12)

### `agentlib/langchain_tools.py` — the tool-wrapping convention
<!-- OWNER: Berat -->
- **Owns:** `to_langchain_tool(fn) -> StructuredTool`, `build_langchain_tools(fns) -> list`.
  The single place a plain Python tool function becomes a LangChain tool.
- **Contract notes:** tool functions are unchanged plain callables — same signature, same
  docstring-as-description, same `Literal[...]`-derived enums, same ambient
  `session_scope`/`impact_scope` reads (invariant #25 is untouched: nothing here adds an
  identity or scope parameter). Tool authors never call LangChain's tool API directly
  (decision #50).
- **Depends on:** `langchain_core`
- **Status:** **done** (Phase 12)

### `agentlib/graph.py` — the LangGraph orchestration core
<!-- OWNER: Berat -->
- **Owns:** `build_graph(tools, registry, approve, ...) -> CompiledGraph` — an `agent` node
  (a LangChain `ChatOpenAI` bound to the Zen base URL via `agentlib.core.BASE_URL`, using
  `bind_tools`) and a `tools` node.
- **Contract notes:** the `tools` node re-hosts `loop.py`'s existing branch logic by calling
  straight into `agentlib/guards.py` (`validate_args`, `requires_approval`, `is_error_result`,
  `detect_stall`, `call_signature`) — no guard logic is duplicated or rewritten, only moved to
  a graph node. The approval gate stays the existing synchronous `approve(name, args) -> bool`
  callback, not LangGraph's native `interrupt()` (decision #52).
- **Depends on:** `langgraph`, `langchain_openai`, `agentlib.guards`, `agentlib.core`
- **Status:** **done** (Phase 12)

### `tools/repo_scan.py` — repository → graph
<!-- OWNER: Alejandro -->
- **Owns:** `scan_repository_structure(root, max_depth, kind)`
- **Writes to:** `store/knowledge_graph.json` — regenerates the DERIVED `nodes`/`edges`
  layer wholesale (decision #16); never touches authored `decisions` (CLAUDE.md §6).
- **How:** two-pass `ast` walk (no regex). Pass 1 emits a node per module
  (`{id, path, kind, symbols}`) with top-level def/class names; pass 2 resolves `import`/
  `from ... import` targets (absolute + relative) and keeps only edges **between known
  nodes** (`relation: "imports"`). Node id = dotted module path, `__init__.py` collapsing
  to its package (decision #17). Reuses `tools.decisions._graph_path/_load_graph/_save_graph`.
- **Reversible → ungated:** a re-scan reproduces the output; no approval ceremony.
- **Contract:** `(root: str, max_depth: int 0..64, kind: Literal["python","markdown","any"])`
  → `{"nodes": int, "edges": int, "root": str, "kind": str, "scanned_at": iso8601}`.
  Structured error branches: `invalid_root` · `invalid_args` (bad `max_depth`/`kind`) ·
  `graph_unreadable` (corrupt graph is refused, never overwritten — decision #12).
- **Status:** **done** (Phase 1, T1.1)

### `tools/graph_query.py` — graph → answers
<!-- OWNER: Alejandro -->
- **Owns:** `query_component_graph(component, relation)`
- **Reads:** `store/knowledge_graph.json`. Read-only, reversible, ungated.
- **How:** resolves `component` by node `id` then by `path`; walks `edges` for the
  requested `relation`. Returns de-duplicated, sorted `related` so an identical query is
  deterministic (matters for the loop's stall guard).
- **Contract:** `(component: str, relation: Literal["imports","imported_by","neighbors","all"])`
  → `{"component", "relation", "found": bool, "node": {…}|None, "related": [id, …]}`.
  `neighbors`/`all` span both directions. An ABSENT/empty graph is a legitimate
  `found: false` answer; a CORRUPT graph is the one error branch (`graph_unreadable`) —
  the distinction is deliberate (decision #18).
- **Status:** **done** (Phase 1, T1.2)

### `tools/decisions.py` — decision log + integrity check
<!-- OWNER: Dias (HW1); reassigned to Berat for HW2 — see TODO.md ownership map -->
- **Owns:** `append_decision_record(...)`, `retrieve_decisions(...)`,
  `verify_graph_integrity(...)`, `migrate_legacy_decisions(...)`, plus the graph-file I/O
  helpers (repo-root-anchored path, call-time `RADF_GRAPH_PATH` override for tests/demos,
  atomic writes) shared with `tools/graph_write` and `tools/repo_scan` (decision #11).
- **HW2 change:** the authored layer moved from `decisions[]` inside the JSON graph to the
  **overlay** (`store/radf.db`, decision #21). The JSON file is now purely derived.
- **`append_decision_record`:** append-only, reversible, ungated. Gains a `visibility` enum
  (`"team"` | `"private"`). `author_id` comes from the **session, never an argument**
  (decision #25) — no session is `{"error": "no_session"}`. Returns
  `{decision_id, symbol_uid, component, decision, rationale, status, visibility, author_id, ts}`.
  Blank fields → `{"error": "invalid_decision_record"}`. A corrupt *graph* file no longer
  blocks it: the two layers now fail independently.
- **`retrieve_decisions(component, scope)`:** read-only pull side. Scoped to the session user
  in SQL (decision #24). `scope="component_and_repo_wide"` (default) also returns decisions
  with `symbol_uid IS NULL`, because dropping the broadest constraints is how you miss one.
- **`verify_graph_integrity`:** unchanged contract, now a **cross-store** check — overlay uids
  joined against structural node uids. Returns uids only, never decision text, so an integrity
  check cannot leak another user's content. Orphans are surfaced, never deleted.
- **Depends on:** stdlib, `overlay`, `agentlib.session`.
- **Status:** **done** (Phase 2 T2.1/T2.2; HW2 Phase 4b)

### `tools/graph_write.py` — destructive graph edits
<!-- OWNER: Dias -->
- **Owns:** `prune_graph_node(...)`
- **Contract:** **irreversible** — listed in `GATED`, requires explicit human approval.
  Success: `{"removed", "edges_removed", "cascade"}`. Unknown node / missing file →
  `{"error": "node_not_found", ...}`; non-JSON file → `{"error": "graph_unreadable"}`,
  never modified. `cascade="node_only"` deliberately leaves orphan edges for
  `verify_graph_integrity` to flag; the authored `decisions` layer is never cascaded —
  a pruned component's decisions become orphans, surfaced not deleted (decision #14).
- **Status:** **done** (Phase 2, T2.3)

### `tests/smoke_hw1.py` — offline smoke tests
<!-- OWNER: Dias -->
- **Owns:** the T2.5 end-to-end suite. The model is SCRIPTED (`agentlib.loop.call`
  monkeypatched with replayed `Result`s); the loop, guards, gate, registry and tools
  are the real code.
- **Covers:** both gate branches (declined blocks the write · approved prunes) · the
  tool-error branch (B2) · the invalid-args branch · `max_steps` · `stalled` · derived
  schema enums · derived-vs-authored invariants. The seeded-graph query test is written
  and skipped pending T1.2. The live-model integration run is T3.2.
- **Status:** **done** (Phase 2, T2.5; scripted-model by design while Zen credits are pending)

### `tools/text_tools.py` — line-diff utility (HW4 new tool #2)
<!-- OWNER: Alejandro -->
- **Owns:** `diff_texts(before, after, mode: Literal["unified","ndiff"]) -> dict`
- **What:** computes the line diff between two text blobs via `difflib` — the second
  framework-integrated tool of the HW4 requirement (Berat's `evaluate_expression` is #1). Pairs
  with `apply_change`: one lands an edit, the other shows exactly what an edit changes instead
  of asserting it in prose.
- **Read-only → ungated** (CLAUDE.md §5): comparing two strings damages nothing.
- **Contract:** `{"mode": str, "changed": bool, "diff": [str]}` on success (`changed=False`,
  empty `diff` when identical); `{"error": "input_too_large", "details": [str]}` over a 200k-char
  combined cap (own branch — never a partial diff dressed as complete).
- **Status:** **done** (Phase 13a)

### `retrieval/types.py` — the chunk contract (HW5)
<!-- OWNER: Berat -->
- **Owns:** `Chunk`, `Hit`, `Anchor`, `EvalCase` (frozen dataclasses), `ChunkKind`, `KINDS`.
- **What:** the shape the retrieval layer (Phase B) and the eval harness (Phase C) are both
  built against, so neither owns it and they can proceed in parallel. **Frozen contract.**
- **Three fields that are cheap now and expensive later:** `symbol_uid` — every chunk joins the
  existing uid space (#22), so a hit can be handed straight to `retrieve_decisions` or the impact
  walk without a second lookup. `heading_path` — prefixed onto chunk text at index time; without
  it a chunk reading "JSON, because it is diffable" is unretrievable by any query not already
  using those words. `Hit.rank` — the position the *retriever* returned, read by MRR/nDCG before
  any repacking (#59).
- **`EvalCase.out_of_corpus`** is `not anchors`: a case with no golden anchors is the deliberately
  unanswerable one, correct as written. Its metrics are undefined rather than zero.
- **Status:** **done** (Phase A)

### `retrieval/` — hybrid search layer (HW5)
<!-- OWNER: Berat -->
- **Owns:** `chunker`, `embed`, `cache`, `store`, `bm25`, `fuse`, `rerank`, `search`, `index`.
- **Pipeline:** chunk (`node_summaries` + decisions + doc sections) → embed
  (`text-embedding-3-small` via OpenRouter, disk-cached by text sha) → retrieve 30 per arm
  (pgvector exact scan ‖ hand-written Okapi BM25) → RRF fuse (k=60) → optional LLM rerank to
  top-k, cached on `(query, candidate ids)`.
- **Seam:** `search(query, *, k, rerank, source) -> list[Hit]`, returning **retriever order**
  (#59). Exact vector scan rather than HNSW/IVFFlat: ~600 chunks does not warrant approximation,
  and saying so is a stronger justification than a copy-pasted `lists=100`.
- **Corpus, as built:** 963 chunks — 351 component cards, 607 doc sections, 5 decisions (3
  team-visible). ~117k tokens, **$0.002** and 41s for a full re-index; ~1s when cached.
- **Status:** **done** (Phase 14B)

### `tools/retrieval_tools.py` — retrieval as a tool (HW5)
<!-- OWNER: Berat -->
- **Owns:** `search_corpus(query, k=5, rerank=True, source: Literal["all","components","decisions","docs"]) -> dict`
- **What:** closes §6's long-standing "retrieval over the graph — exact lookup only" gap. A
  request phrased the way people phrase them ("make the approval prompt clearer") matches
  nothing through `query_component_graph`, which needs a dotted module id. Listed ahead of the
  two exact lookups in `TOOL_FUNCTIONS`: ranked guess first to *find* the name, exact join second
  to answer about it.
- **A tool, not a pipeline stage** (#60) — the model decides whether to search, and can refine
  and re-query, which needed no new machinery because `detect_stall` (#10) blocks only identical
  repeats. Read-only → ungated. No identity/scope parameter (#25).
- **Contract:** `{"query","k","reranked","source","count","results":[{chunk_id, kind, symbol_uid,
  symbol, heading_path, source_path, rank, score, text}]}`; errors
  `index_unavailable | index_empty | invalid_args` get their own branch (Part B, B2). A
  `source=` filter matching nothing is an empty result, **not** `index_empty` (decision #64).
- **Status:** **done** (Phase 14B)

### `overlay/summarize.py` — the summariser (HW5)
<!-- OWNER: Berat -->
- **Owns:** `run(...)`, `summarize_node(...)`, `python -m overlay.summarize`.
- **What:** fills `node_summaries` — one module card plus one card per symbol **the scanner
  declared**. Model-proposed names absent from that list are dropped (model proposes, code
  decides, #38). Idempotent on `content_sha`, so an unchanged file costs no call.
- **Run:** 57 nodes -> 342 cards, 0 failures. Uses a brace-depth JSON slice, not `rfind("}")` —
  the defect filed against `agents/planner.py::_brace_slice` would have cost ~1 node in 4 here.
- **Status:** **done** (Phase 14B)

### `eval/` — retrieval + generation evaluation harness (HW5)
<!-- OWNER: Alejandro (Phase 14C, Part 2); Dias (Phase 14D, Part 3) -->
- **Owns:** `cases.json`, `retrieval_metrics.py`, `generation_metrics.py`, `run_eval.py`.
- **Separate from agent code on purpose** — the harness imports the retrieval layer, never the
  reverse.
- **Rank-aware metrics** (free, reproducible, run first): hit rate@k, precision@k, recall@k, MRR,
  nDCG@k. No eval library ships this family. Empty-golden cases are **undefined, not zero** —
  excluded from averages and counted separately.
- **Judged metrics** (an LLM call each): hand-rolled on `monitor/judge.py`'s pattern — named
  values, never a 1-10 score (#37, #61) — and disk-cached.
- **Goldens are content anchors, not chunk ids**, resolved at load: ids move on every re-chunk,
  and a golden set that silently goes stale is worse than none.
- **Part 2 (done, Phase 14C):** `retrieval_metrics.py` (the five metrics, stdlib-only, empty-golden
  = `None`; #67), `loader.py` (anchor resolution — component→uid, decision→record number,
  doc→heading; a case whose anchors all miss is `unresolved`, excluded and counted), `cases.json`
  (24 cases, 7 categories incl. 2 out-of-corpus, reusing Part 1's 10 queries), `run_eval.py` (the
  k∈{3,5,10} × rerank∈{on,off} matrix with per-category breakdown, over an injectable `search_fn`
  so the matrix is offline-testable). `agents/planner.py` seeds via `search_corpus` (#66).
- **Status:** **Part 2 done** (Phase 14C); Part 3 (`generation_metrics.py`, `gen_cases.json`) **stub** (Phase 14D)

### `tools/__init__.py` — registry + schema assembly
<!-- OWNER: Berat; HW4 registry-assembly additions by Alejandro (Phase 13a, decision #54) -->
- **Owns:** `build_registry() -> (schemas, registry)`, module-level `SCHEMAS` / `REGISTRY`.
  Imports every tool stub so the loop runs end-to-end from day one.
- **HW4 (decision #54):** `build_langchain_registry() -> list[StructuredTool]` and module-level
  `LANGCHAIN_TOOLS` — the whole `TOOL_FUNCTIONS` list run through the one conversion point
  (`agentlib.langchain_tools.build_langchain_tools` → `to_langchain_tool`, decision #50). This
  is the explicit, testable registration surface; `run_agent` does the identical conversion
  internally from its `registry` arg, so its frozen signature (#49) is untouched. No tool body
  or signature changed — all ten convert cleanly.
- **Status:** **done** (Phase 0); LangChain registration surface added (Phase 13a)

### `main.py` — CLI entry point
<!-- OWNER: Berat -->
- **Owns:** argument parsing, `.env` load, registry assembly, `input()`-based approval callback
  (fail-safe: only an explicit `y` approves).
- **Phase 0 DoD:** `python main.py "..."` runs the full loop and fails only with
  `NotImplementedError` from the stubs (verified against a stubbed `call`; a live run also
  needs the Zen key in `.env`).
- **Status:** **done** (Phase 0)

---

## 4. Data contracts

### `store/knowledge_graph.json`
<!-- OWNER: Alejandro defines; Dias consumes -->
```jsonc
{
  // nodes: one per module. id = dotted module path ("agentlib.core"); __init__.py
  // collapses to its package id ("tools"). markdown nodes use the posix relpath as id.
  "nodes":     [ { "id": "agentlib.core", "path": "agentlib/core.py", "kind": "python", "symbols": ["call", "Result"] } ],
  // edges: internal import edges only (both endpoints are known nodes). relation = "imports".
  "edges":     [ { "from": "agentlib.loop", "to": "agentlib.core", "relation": "imports" } ],
  "meta":      { "scanned_at": "<iso8601>", "root": "<scan root as passed>" }
}
```
_`kind` ∈ {"python","markdown"}. **As of HW2 this file is purely DERIVED** — the `decisions[]`
key is gone (decision #21). A scan that finds a legacy `decisions[]` migrates it into the
overlay before dropping it, so a scan can never be the thing that loses authored knowledge._

### `store/radf.db` — the overlay (authored)
<!-- OWNER: Berat -->
```sql
decisions(decision_id PK, symbol_uid, visibility, author_id, decision,
          rationale, rejected, status, supersedes, ts)
runs(run_id PK, user_id, thread_id, agent, request, started_at, ended_at, stopped, log_path)
run_scratch(seq PK, run_id, agent, step, key, value, ts)          -- append-only
scratch_reads(seq PK, run_id, agent, step, key, saw_seq, ts)      -- incl. misses
silences(silence_id PK, run_id, trigger, reason_code, evidence,   -- HW3, T9.4
         visibility, ts)
node_summaries(symbol_uid, symbol, PK(symbol_uid, symbol),        -- HW5, T14.1
               summary, responsibility, signature, content_sha,
               author_id, source_run_id, updated_at)              -- UPSERTed
```
`node_summaries` is authored prose about a *derived* node, which is why it is here and not in
`knowledge_graph.json`: decision #16 lets any scan replace that file wholesale, so a summary
stored in a node is a summary the next scan deletes (decision #57). `symbol = ''` is the module
card; a non-empty `symbol` is a per-symbol card. Unlike every other table here it is UPSERTed
rather than append-only — a summary is a current description, not a historical claim, and
retaining superseded wordings would fill the retrieval corpus with near-duplicates of itself.
`content_sha` is what lets **code** decide a summary is stale (the file changed) without asking
a model; a `symbol_uid` that no longer resolves is **orphaned, surfaced, never deleted**, same
rule as decisions. No `visibility` column, deliberately: a decision can be private (#24), but a
description of what `agentlib/guards.py` does is a fact about shared code, and scoping it would
imply a boundary the source tree does not have.

### `RADF_PG_DSN` — the retrieval index (derived)
<!-- OWNER: Berat defines; Alejandro builds -->
```sql
chunks(chunk_id PK, kind, text, embedding vector(1536),
       symbol_uid, symbol, heading_path, source_path, content_sha, visibility)
```
Postgres + pgvector, from `docker-compose.yml` (port 5433, so it can never collide with a local
Postgres). **Entirely derived** — droppable and rebuildable at any time, and the sqlite overlay
is explicitly not migrating into it (decision #62). `kind` ∈ {`component`,`decision`,`doc`} and
doubles as `search_corpus`'s `source` filter. Only `decision` chunks ever carry a non-`team`
`visibility`, and it is applied as a `WHERE` clause on the same query as the vector scan (#24) —
never as a post-filter and never as an instruction to the model. Chunk and hit shapes are frozen
in `retrieval/types.py`.
`symbol_uid` is `resolve_uid(component)` — `"Kind:path"`, e.g. `"Module:agentlib.core"`.
`NULL` means repo-wide. `visibility` is `"team"` or `"user:<id>"` — on `silences` too, and
there it names who may read the *reason* (decision #44), which for the leak guard is the
owner of the withheld decision rather than the person who asked.

### `store/memory.json` — free-form memory (authored)
<!-- OWNER: Berat -->
```jsonc
{ "memory_id": "m_…", "kind": "fact" | "rule", "visibility": "team" | "user:<id>",
  "cue": ["pytest", "fixture"], "applies_to": "Module:…" | null, "text": "…",
  "source": { "author": "berat", "session_id": "…", "quoted": true },
  "status": "proposed" | "accepted", "created_at": "…",
  "last_used_at": "…" | null, "use_count": 0 }
```
`source.quoted` is what lets the renderer quote it instead of obeying it.

**Invariant — structure and decisions are separate layers.** `nodes` and `edges` are *derived*: any
scan may regenerate them wholesale, and nothing outside the scanner may hand-edit them. Decisions
are *authored*: they are the durable knowledge the project exists to accumulate, and no scan may
overwrite them. As of HW2 the two live in **two different files**, so the separation is enforced by
the filesystem rather than by the scanner remembering to preserve a key. They are joined by
`symbol_uid`, never merged — a decision references a node, it is never stored *inside* one. This
keeps the decision layer portable when the structural half is replaced by an external indexer (§6).

A decision whose `symbol_uid` no longer resolves to a node is **orphaned**, not deleted: the
component moved or was removed, so the decision may be stale and should be surfaced for review.
`verify_graph_integrity` reports orphans as a structured error rather than dropping them.

---

## 5. Decision log

> Append one entry per non-obvious choice. Newest last. Do not delete entries — supersede them.

| # | Date | Component | Decision | Rejected alternative | Why |
|---|------|-----------|----------|----------------------|-----|
| 1 | | repo-wide | Knowledge graph stored as a single JSON file | SQLite, graph DB | HW1 forbids extra deps; JSON is inspectable and diffable in PRs |
| 2 | | agentlib | Raw Python loop, no framework | LangGraph | HW1 constraint; framework refactor is Session 9 — **superseded by #49** (HW4: the framework refactor this decision deferred) |
| 3 | | tools | `prune_graph_node` is the only gated tool | gating all writes | Irreversibility decides the gate; append/scan are recoverable |
| 4 | | indexing | GitNexus selected as the future structural indexer (post-HW1) | CodeGraph; continuing with hand-rolled `ast` scanning long-term | GitNexus stores the graph in an embedded graph DB (LadybugDB) and exposes a raw `cypher` tool plus a published schema resource, so custom entities and traversals are supported through the public API. CodeGraph is SQLite/FTS5 behind a single `codegraph_explore` tool — extending it means reaching into internals its own file-watcher continuously rewrites. |
| 5 | | indexing | Decisions live in a **separate overlay**, joined to structure by `symbol_uid` — never written into indexer-owned nodes | enriching the indexer's nodes in place with decision metadata | Both candidate indexers re-index aggressively (CodeGraph on every file event, GitNexus on `analyze`). Anything injected into their extraction output is overwritten on the next sync, and couples our core contribution to their internal schema. The overlay makes re-indexing free and makes a stale reference visible as an orphan rather than a silent loss. |
| 6 | | licensing | GitNexus is PolyForm Noncommercial; CodeGraph is MIT | — | Acceptable for coursework, recorded because it forecloses commercial use of this repo downstream. Revisit if the project outlives the course. |
| 7 | 2026-07-23 | agentlib | Runtime module is `agentlib/core.py`; notebooks' `agentlib.tools` surface is preserved unchanged | Renaming the notebook import path in code; forking a second module | The ownership map (TODO) and component list (§3) authoritatively name `core.py`. Same functions (`call`, `Result`, `show`, `CHEAP`, `STRONG`, `MODELS`, `estimate_cost`). If a session needs the notebook path verbatim, add a one-line `agentlib/tools.py` re-export rather than duplicating logic. |
| 8 | 2026-07-23 | schemas | `schema_for` derives `enum` from a `Literal[...]` param annotation | Requiring every author to hand-add enums after the fact | The tool stub signatures already declare `Literal[...]` for their constrained params; deriving the enum just reads the annotation the author wrote. Authored narrowing beyond the signature (numeric bounds, when-not prose) still sits on top. Needed `get_type_hints` to resolve PEP 563 string annotations. |
| 9 | 2026-07-23 | loop | `truncated` is a first-class stopping condition on the model's OWN output | Treating returned text as an answer whenever it is non-empty | "It returned" ≠ "it finished" (Part B, B1 guard 2). Truncated text is routed to an error branch, never fed back as data. |
| 10 | 2026-07-23 | loop | Stall = a repeated identical (name+args) call; a repeat of a *declined* call stops as `declined`, not `stalled` | Making a decline immediately terminal; letting the model spin forever | A decline first returns a `declined` result so the model can react and answer (Part B, B4, TODO T2.4). Only if the model re-issues the same blocked call does the loop stop — reported as `declined` (blocked action) vs `stalled` (general spin) so the trace says which. |
| 11 | 2026-07-23 | tools (Phase 2) | Graph path is repo-root-anchored with a call-time `RADF_GRAPH_PATH` env override; writes are atomic (temp file + `os.replace`) | cwd-relative path; direct `json.dump` over the live file | CLI and tests run from different cwds; a crash mid-write must not truncate the file carrying the authored decisions layer |
| 12 | 2026-07-23 | tools (Phase 2) | An unreadable (non-JSON) graph file is refused with `{"error": "graph_unreadable"}` — never recreated or rewritten | silently recreating the file from the empty shape | Recreating would destroy authored decisions — the one layer no process may regenerate (CLAUDE.md §6) |
| 13 | 2026-07-23 | decisions | Orphan-decision check joins `component` against node ids ∪ node paths, and is skipped while `nodes` is empty | flagging every decision on an unscanned graph | Pre-scan, every decision would false-flag as orphaned — noise burying signal. Id-or-path join tolerates either id convention until T1.5 finalises it |
| 14 | 2026-07-23 | graph_write | `cascade` stays **required with no default**; `node_only` leaves orphan edges for `verify_graph_integrity` to flag; the authored `decisions` layer is never cascaded (closes the TODO open question) | defaulting to `node_and_edges`; cascading decisions too | The model must state blast-radius intent explicitly; orphaned edges/decisions are surfaced by verify, not silently swept — pruning structure must never delete authored knowledge |
| 15 | 2026-07-23 | agentlib | `CHEAP = gpt-5.4-nano`, `STRONG = gpt-5.5`; `MODELS` keyed by **literal model id**, not by the `CHEAP`/`STRONG` variables | Keying the price table by the `CHEAP`/`STRONG` symbols as originally stubbed | Both ids are env-overridable, so variable-keyed entries silently become the *wrong* prices under the *right* key the moment `.env` changes — and `estimate_cost(usage, model)` takes an arbitrary id anyway. Literal keys make an unpriced model miss the lookup and return `0.0` (visibly wrong) instead of returning a confidently wrong number. `gpt-5.5` is priced at its ≤272K context tier only; longer contexts bill higher and are not modelled. |
| 16 | 2026-07-24 | repo_scan | A re-scan **replaces** the derived `nodes`/`edges` wholesale (decisions preserved) | merging new scan results into the existing derived layer | Closes the TODO open question. "Structure is derived" (CLAUDE.md §6) means a scan is the single source of truth for structure — merging would let a deleted/renamed module linger as a stale node forever. Replacement makes the graph reflect the tree exactly; the authored `decisions` layer is loaded and written back untouched, and a now-dangling decision surfaces as an orphan via `verify_graph_integrity`, not as silent corruption. |
| 17 | 2026-07-24 | repo_scan | Node id = **dotted module path**, `__init__.py` collapsing to its package id; markdown uses the posix relpath | filesystem path as id; keeping `__init__` in the id | The dotted form is what `import` statements name, so edge resolution is a direct string match against import targets — no path↔module translation. It also matches the ids the smoke fixtures and decision #13's id-or-path join already assume. Edges are kept only between known nodes, so imports of stdlib/third-party modules don't create dangling edges. |
| 18 | 2026-07-24 | graph_query | An **absent/empty** graph is a valid `found: false` answer; only a **corrupt** graph is an error branch | raising/erroring whenever the component isn't found | Emptiness and corruption are different failures. "Not in the graph" is a real, useful answer the model acts on (the docstring steers it to scan). A non-JSON file is genuine corruption the loop must branch on (Part B, B2) rather than mistake for "no results". Keeping the tool total (never raising) is what lets the loop own every stopping decision. |
| 19 | 2026-07-23 | agentlib | Gemini ids are excluded from selection despite appearing in Zen's `/models` list | Using `gemini-3-flash` as `CHEAP` (its listing implies support) | Zen 400s on every `gemini-*` id via both `/responses` and `/chat/completions`: `Invalid JSON request body: Missing key at ["contents"]`. `contents` is Google's native field, so Zen forwards our OpenAI-shaped body untranslated — a provider-side gap, not fixable here. Verified reproducible on `gemini-3-flash` and `gemini-3.5-flash-lite`, with `gpt-5-nano` succeeding on the identical code path. **A model appearing in `/models` is not evidence it works; smoke-test before pinning.** |
| 20 | 2026-07-23 | core | `_to_result` sanitizes `output_items` for replay: **drop `reasoning` items** and **strip the server `id`** from each item before it is stored | Storing raw `model_dump()` items and replaying them verbatim | `output_items` is fed back as the next turn's input by the loop, so it may only hold items the Responses *input* schema accepts. Verified by controlled replay against `gpt-5.5`: replaying a `reasoning` item, or a `function_call` carrying its server-assigned `id`, both 400 with `Error from provider (Console): Upstream request failed`. Only dropping the reasoning item **and** stripping `id` (keeping `call_id`, the tool correlator) succeeds. The `gpt-5.4-nano` upstream tolerated both, so the bug surfaced **only on STRONG** with identical loop code — a reminder that provider strictness is model-specific (cf. decision #12). `Result.raw` still holds the untouched response. |


### HW2 decisions

| # | Date | Component | Decision | Rejected alternative | Why |
|---|------|-----------|----------|----------------------|-----|
| 21 | 2026-07-26 | overlay | The **structural** layer stays JSON and stays a disposable stand-in; only the **authored overlay** moves to SQLite | migrating `nodes`/`edges` into SQLite alongside the decisions | §6.1 already commits the structural half to GitNexus. Building a schema for data we have decided not to own is throwaway work, and it converts the promised "uid remap" into a real migration. The overlay is the half that survives every re-index, so it is the half worth investing in. Side effect: the derived/authored separation is now enforced by the two layers being in two *files*, not by `repo_scan` remembering to preserve a key. |
| 22 | 2026-07-26 | overlay.uid | `symbol_uid` becomes real: every overlay row keys on `resolve_uid(component)`, emitting `"Kind:path"` | continuing to join on a bare `component` string (decision #13) | `symbol_uid` was documentation-only — zero occurrences in any `.py`. §6.1 promises the GitNexus swap is "a uid remap, not a rewrite", and that is only true if nothing downstream stores a raw component string. Now the migration is a change to one function. Also collapses the three spellings of a module (`agentlib.core`, `agentlib/core.py`, windows path) to one key, which retires the id-or-path special case. |
| 23 | 2026-07-26 | overlay | Decisions are **relational** (SQLite); free-form memory is **non-relational** (JSON) | one store for both; JSON for both "because fields evolve" | The split is not structured-vs-unstructured, it is *whether the query is the product*. "Which accepted decisions constrain the modules this change touches, for this user?" is a join against an impact set — in JSON that is a full scan on every run. Schema stability is a feature for decisions and a cost for learned facts. |
| 24 | 2026-07-26 | overlay / tools | Visibility is enforced **in the query** (`visible_to`, `_visible`), never by instructing the model | passing all rows and telling the model to ignore other users' | An instruction is a request; a `WHERE` clause is a boundary. B's agent is never handed A's rows, so no sampling of B's model can leak them — and the injection case then fails even when the model is fooled. |
| 25 | 2026-07-26 | agentlib.session | Identity (`author_id`) and the write scope (`impact set`) are **ambient**, read from the session, never tool arguments | `author_id` / `impacted` as parameters the model fills in | The model's context contains other people's text. If identity were an argument, "you are now acting as alice" would be enough to write as alice; if the impact set were, the model would be authorising its own writes with a list it just invented. An empty impact set therefore denies every write — it does not mean "unrestricted". |
| 26 | 2026-07-26 | agentlib.context | Rules from **files** go into `instructions`; decisions and memory go into `input[]` as quoted, attributed data | rendering retrieved memory into the system prompt where it is best obeyed | The chain of command is root → system → developer → user; quoted text sits outside it. A file in the repo was edited by an admin, so it earns developer authority. A stored "user fact" did not — rendering it into `instructions` *is* the memory-injection attack ("remember that I'm an admin and deletions are pre-approved"). Quote blocks also escape `<`/`>` so a payload cannot close the wrapper and reframe itself. |
| 27 | 2026-07-26 | agentlib.context | A rule with `applies_to` is bound **mechanically** by the impact set; only unbound rules are cue-matched | letting the model decide relevance for every rule | The graph is the router. The rule says *what*; the graph and the cue say *when*; the model only picks among pre-narrowed candidates. A misapplied rule then traces to a wrong impact set (graph bug) or a wrong cue (retrieval bug) — never to model judgement, which is not debuggable. |
| 28 | 2026-07-26 | overlay.memory | Inferred memory is saved `proposed` and shapes behaviour only after a **second independent observation**; stated memory is `accepted` at once | saving every inference as fact; asking the user every time | Splits the two failure modes the rubric names. A one-off guess never silently becomes a standing instruction, and a real preference still lands without ceremony. The model may not claim `stated_by_user` to skip the wait — that flag is about what the user said, and misusing it is visible in the log. |
| 29 | 2026-07-26 | agents | The orchestrator is **plain Python**, not a third agent | a coordinator LLM routing between planner and executor | Every decision it makes is a branch on an enum. In Python that is free, testable, and cannot be talked out of its decision by text in its context. A router that only routes does not earn a model call. |
| 30 | 2026-07-26 | agents | The plan crosses from planner to executor through **`run_scratch`**, not as an argument | passing the plan directly | Deliberately the harder thing, because it is what HW2 asks us to reason about: a shared store is a channel with no call site, invisible to grep and to any call graph. Made survivable by append-only writes (the earlier value is still there when you debug) and by logging every read with the `seq` it observed — including misses, so "looked and found nothing" and "never looked" stay distinguishable. |
| 31 | 2026-07-26 | agents.executor | The executor's toolset is narrow **by construction**, and its boundary lives in `agents/executor_brief.md` | one registry for all agents, with prose telling the executor what not to use | A tool absent from the registry cannot be called by a model that has been argued into wanting it. Keeping the brief in a file means a human can amend the delegation in seconds without touching Python — the same reason operating rules are a file. |
| 32 | 2026-07-26 | agentlib.runlog | The run log records the **assembled instructions**, not just the actions | logging tool calls and the final answer only | "The agent ignored a rule" and "the rule was never in its context" produce identical traces and have opposite fixes — one is a model failure, the other is a bug in the assembler. The judge runs after the fact and cannot re-run anything, so if the distinction is not in the log it cannot be made at all. |
| 33 | 2026-07-26 | tests | `tests/conftest.py` redirects all four stores by autouse fixture; `smoke_hw1.py` renamed to `test_smoke_hw1.py` | per-test redirection as in HW1 | Three stores is past the point where "remember to redirect it" scales — one forgetful test writes into the developer's real overlay and nothing fails to say so. The rename fixed a suite that pytest's `test_*.py` discovery had never collected: it only ever ran when named explicitly, so its 23 tests were silently absent from `pytest tests/`. |
| 34 | 2026-07-26 | agents.planner | The impact walk is **code-owned and capped** — the model is called once (seed + steps from free-form text), then pure Python does the transitive `imported_by` BFS to `max_hops` | letting the model call `query_component_graph` in a loop to discover impact | Same rule as stopping conditions (§5): the walk is the agent's answer to "what breaks", so it must be the code's decision, not talked out of the cap by context. An uncapped walk on a real repo reaches everything (15 modules at 2 hops from `tools.decisions`) and permits every write. `max_hops` is recorded in the plan as `impact_max_hops`, so the cap is visible in the trace. |
| 35 | 2026-07-26 | agents.planner | A **conflict** = two live decisions on one `symbol_uid` where one's `decision` is the other's `rejected` alternative → `open_questions` (stops the run) | letting the model judge whether decisions contradict; or ignoring conflicts | `open_questions == []` authorises the executor to act without a human, so it must be earned by a check the code can make and a test can pin. Semantic contradiction needs the model and is not reproducible; the `decision`-vs-`rejected` join is exact, deterministic, and seeded straight into the overlay in a test. |
| 36 | 2026-07-26 | tools.apply_change | Both confinements — path (repo root + `DENYLIST`, resolve-then-compare) and impact-set (against the ambient `current_impact_set()`) — live **in the tool**, never in the prompt | trusting the plan's paths, or gating on the human approval alone | The gate answers "should this irreversible thing happen"; it cannot bound *what* the write touches, because by the time a human reads the prompt they are approving a path the model chose. The impact set is ambient for the same reason `author_id` is (#25): a parameter would let the model authorise its own writes. Empty impact set = deny all. A refused write leaves the file byte-identical (check first, write second). |
| 37 | 2026-07-27 | monitor.judge | The judge grades on **named values** (`strictly_adheres`/`minor`/`serious`, `grounded`/`partial`/`ungrounded`), never a 1–10 score | a numeric score, or a single pass/fail | A number hides the reason and invites averaging away a serious breach with three clean runs. A named value forces the line — "minor leaves the outcome unchanged; serious crossed a boundary" — to be drawn in `rubric.md`, editable by an admin, not buried in the model's head. |
| 38 | 2026-07-27 | monitor.judge | The model **proposes** violations; the **code decides** the verdict. A violation with no `expected`+`observed` is dropped before reporting; the final adherence label is computed from the survivors, not read from the model's self-report | trusting the judge's own verdict and severity | The judge is itself an LLM and hallucinates. An unverifiable verdict is indistinguishable from a hallucination (T7.3a), so it is discarded, not trusted — the same principle as HW1's guards: never take the model's word, require a receipt. Grounding downgrades are trusted only when they carry a rationale, else treated as grounded. |
| 39 | 2026-07-27 | monitor.judge | "The agent ignored a rule" and "the rule was never in its context" are **split by the code** using `assembled.instructions`: a cited rule absent from the pushed prompt is an **assembler gap**, never a model adherence violation (T7.3c) | reporting every unmet rule as a model failure | The two have different fixes — one is the model, the other is the context assembler. Conflating them sends the fix to the wrong owner. `rule_in_context` checks the rule id (and a quoted snippet) against the full pushed prompt; only in-context violations count toward `prompt_adherence`, gaps are surfaced separately. This is why the run log carries `assembled.instructions` in full (runlog.py). |
| 40 | 2026-07-27 | monitor.judge | The monitor is **read-only and isolated**: a separate job on its own clock, over `runs.jsonl`, with no tools, no live store, and no way to affect the run it grades | a verification step inside `run_agent`, or a judge with store access | A judge that can act can be steered by the same injection that steers the agent (R4), and a judge inside the loop cannot grade the loop's own stopping decision. Reading only the frozen log record keeps the grade independent and the run replayable. |


### HW3 decisions

Numbers **#41–#45** are Phase 9 (Berat); **#46** is reserved for Phase 10 (Alejandro) and
**#47–#48** for Phase 11 (Dias). Pre-allocated so three parallel branches do not collide in
this table — the renumbering in T3.4/T8.4 was the lesson.

| # | Date | Component | Decision | Rejected alternative | Why |
|---|------|-----------|----------|----------------------|-----|
| 41 | 2026-07-28 | agentlib.approval | The approval gate becomes **asynchronous and in-channel** for the channel path: the answer arrives as another message, only the requester's counts, and a timeout **declines** | keeping `input()` and running the worker synchronously; auto-approving after a wait | This is the one HW2 contract HW3 changes, so it is recorded rather than filed as a refactor. `input()` on a worker thread reads a stdin nobody is attached to and hangs the only worker there is. What *doesn't* change is the whole point: `GATED`, the loop's gate branch, the `(name, args) -> bool` callback shape and `main.py`'s CLI gate are all untouched, so every HW1/HW2 test still exercises the gate it was written against. Only the requester answers, in the thread they were asked in — a shared channel means anybody can type "y", and a bystander must be able to neither approve *nor* cancel someone else's write. Timeout declines because an unanswered gate that eventually proceeds is a delay, not a gate; shutdown declines for the same reason — it is not consent. |
| 42 | 2026-07-28 | channel.queue | **One worker.** Turns are strictly serialised | a worker pool, or one worker per thread_key | Not a throughput compromise — a correctness constraint. Identity and write scope are ambient `contextvars` (#25): they are what keeps A's private rows away from B and what authorises a write. Concurrent turns would each need their own context, and the failure mode of getting that wrong is not a slow reply, it is B's agent acting as A. One worker makes the race structurally impossible instead of carefully avoided. The cost is real and named in the write-up: throughput is one turn at a time, and a long turn blocks the queue. |
| 43 | 2026-07-28 | channel.queue | **Per-path admission**, not one uniform strategy: webhook → coalesce, heartbeat → drop the duplicate, human → queue but **reject with a reason** while a gate is open | strict FIFO for everything; coalescing everything; interrupting the in-flight turn | The three paths want different things and one rule would be wrong for at least two. A push's answer is read off the working tree, so only the newest event was ever going to be right — coalescing is correct, and it *costs* per-commit granularity (we can say a decision went stale in this batch, not which commit did it). A queued heartbeat already covers everything outstanding, so the duplicate is dropped rather than coalesced, and that costs nothing — which is why it is a different rule. A human's message is never merged or dropped: discarding a question somebody typed is data loss wearing a policy's clothes. The one exception is while the worker is parked on an approval, where rejecting *with an explanation* costs the human a re-send and buys a bounded, visible wait instead of a message that appears to have been swallowed. Interrupting was rejected outright: it abandons runs mid-way and leaves partial run logs and orphaned gates. |
| 44 | 2026-07-28 | overlay / channel.silence | **Silence is a first-class recorded outcome** with its own table, its own closed reason-code set, and **its own visibility** | logging it as a run with `stopped="silent"`; not recording it at all | "The agent didn't answer" and "the agent decided not to answer" are the same observation and different events; without a value distinguishing them, a deliberate non-answer is indistinguishable from a crashed worker, and the first person to debug one will "fix" the other. The reason must also be *retrievable* or "silence with a reason recorded" is just silence — hence a table and `inspect_store.py silences`, not a note in a log line. Its own `visibility` column because the most important silence, the private-decision leak guard (T11.2), must be readable by the **owner** of the withheld decision and by nobody else: a silence log everyone could read would announce exactly what the silence was protecting. For the same reason `evidence` records what was *checked* — uids, counts, who asked — never the content withheld. |
| 45 | 2026-07-28 | channel.identity | The bot has **its own `author_id`** (`bot:radf`), an unmapped sender resolves to the **anonymous identity rather than to itself**, and read-only is enforced by the empty impact set (#25) rather than by prompt | reusing the platform user id as the RADF user id; trusting the display name; a "you may not write" instruction | "Disposable identity" is only half a token question. The other half is in the store: anything the bot authors must be attributable to the bot, not to whoever asked for it. The platform id is trusted *as a lookup key only* — reusing it directly would let the first stranger to message the bot pick their own `user:<scope>`, one collision away from somebody's private rows. Display names are attacker-controlled and carry nothing. And the read-only posture is doubled deliberately: the write tools are absent from the anonymous registry **and** `append_decision_record` independently refuses a falsy author with `no_session`. Two mechanisms, because the second one is a falsy-string coincidence and a coincidence is a bad thing to hang a trust boundary on. |
| 46 | 2026-07-29 | triggers.webhook / triggers.orphan_watch *(Alejandro)* | The webhook **verifies then enqueues, and does nothing else**; the rescan + orphan diff run on the one worker. And the orphan set that decides whether to speak is a **code-computed diff against a persisted watermark**, surfaced by `symbol_uid` and commit range **only** | scanning inline in `do_POST`; letting the model read the integrity report and decide what to say; naming the orphaned decision's text or author in the notice | "Anyone with the URL can call your webhook" — so an unauthenticated body must not get to pick how much work the box does, and the HTTP thread must not become a second place ambient identity is set (it isn't, because github events carry `external_user_id=None`). Verify-then-enqueue is the same "producers append, the agent consumes at its own pace" the queue is built on. The count comes from code because "how many decisions newly went stale" is a set difference, not a judgement: the model never sees the report, so it can neither miss an orphan nor invent one, and re-running the same push is idempotent because the watermark already holds it. The notice carries the uid and the commit range and stops there: a `symbol_uid` is a structural path, but a decision's *content* or *owner* can be private, and a push is a shared-thread event — surfacing "there is a private decision here, owned by X" is the same leak the T11.2 guard exists to prevent (#24, #44). Surfaced for review, never deleted (§6): the component almost certainly just moved, which is the signal, not an error. |
| 47 | 2026-07-29 | channel.silence | The leak guard is decided from the **visibility-filtered query result** (a comparison of two counts), and it withholds **metadata — existence and ownership — as well as content** | handing the model both row sets with an instruction to keep one quiet; or answering "there's a private decision about that, ask X" | Decision #24: scoping is a WHERE clause, not a prompt. `evaluate_silence` replicates `visible_to`'s predicate over the candidate rows and stays silent when the asker's view is empty while the unfiltered view is not and every row is private to another user. It is not a judgement call and must not become one. The subtly-wrong "ask X" answer leaks less and is still a leak: it discloses that a record exists and who owns it, and existence is content. So the silence record is scoped to the **owner** (`user:<owner>`) — the owner learns someone looked, the asker learns nothing — and `evidence` carries only uids, counts and the asker, never the withheld text (a guard that audits the secret has moved the leak, not closed it). |
| 48 | 2026-07-29 | agents.admin | The admin registry is **narrow by construction** (an explicit `ADMIN_TOOLS` list, never `build_registry()` filtered), and the path is gated on admin identity **AND** an explicit in-channel confirmation — both, never either alone | a filtered full registry; an allowlist as sufficient authority | A filter is one bug away from being the full registry; a list is not — the registry contents are the privilege boundary, so they are stated, not computed-then-narrowed. And an allowlist alone makes *every* message from an admin a privileged one, including the ones they did not mean that way: identity says *who may*, confirmation says *they mean this one*. `admit()` checks both before the model or a store is touched, so a refusal spends nothing. Write scope is still the ambient impact set (#25) — empty denies every write — granted per run, and the loop's approval gate on `apply_change`/`prune_graph_node` is not removed for admins. |

### HW4 decisions

Numbers **#49–#53** are Phase 12 (Berat, blocking). Reserved for Phase 13: **#54** (Alejandro,
tool conversion sweep + second new tool), **#55** (Dias, test-convention sweep + executor/
admin/service verification) — pre-allocated so the two parallel branches don't collide, same
reason #41/#46/#47 were pre-allocated in HW3.

| # | Date | Component | Decision | Rejected alternative | Why |
|---|------|-----------|----------|----------------------|-----|
| 49 | 2026-08-02 | agentlib.loop | LangGraph adopted for orchestration; `run_agent`'s signature and return shape (`{"answer","steps","trace","stopped","run_id"}`) are **frozen unchanged** — the framework swap is internal to `loop.py` | Changing `run_agent`'s contract to something more "native" to LangGraph (e.g. returning the raw final state) | `run_agent` is imported by `agents/executor.py`, `agents/admin.py`, `service.py`, and `main.py`. Freezing the contract makes "preserve all first-half functionality" true by construction — every existing caller needs zero code changes — rather than something re-verified caller by caller. |
| 50 | 2026-08-02 | agentlib.langchain_tools | Tool functions stay plain Python callables; `to_langchain_tool(fn)` is the **one** conversion point | Having each tool author write a LangChain `@tool`/`StructuredTool` directly | Ambient identity/scope (`session_scope`, `impact_scope`, decision #25) is already not a function parameter, so nothing about LangChain's calling convention threatens it — but only if tool authors keep writing plain functions instead of learning a second, LLM-facing argument-schema API where the temptation to add an `author_id` "for testability" would reappear. One conversion point makes the convention enforceable in one file instead of by review discipline across eight. |
| 51 | 2026-08-02 | agentlib.graph_state | State schema is `AgentState`, a `TypedDict` (`messages` + `add_messages` reducer, `trace`, `signatures`, `declined_signatures`, `step`, `stopped`, `answer`) | An ad-hoc dict threaded through node functions | Explicit state schema is the professor's stated requirement, not just style. The fields are exactly what `loop.py`'s local variables already tracked — the schema documents an existing shape, it does not invent a new one. |
| 52 | 2026-08-02 | agentlib.graph | The approval gate stays the existing synchronous `approve(name, args) -> bool` callback; LangGraph's native `interrupt()` is **not** adopted in this increment | Migrating the gate onto `interrupt()` + a checkpointer | HW3 already solved the async/human-in-the-loop case one layer up (`agentlib/approval.py`, `channel/*`, decision #41) for the channel path specifically. Swapping the gate mechanism inside the loop itself would ripple into `service.py`/`channel/*` for a requirement ("explicit state schema", "framework-integrated tools") that doesn't ask for it. Revisit if/when the channel path itself moves onto LangGraph. |
| 53 | 2026-08-02 | repo-wide / CLAUDE.md §4 | The HW1 no-framework rule is lifted for `langgraph`, `langchain`, `langchain-openai` **only**, recorded as a CLAUDE.md §4 amendment | Lifting the rule wholesale; forking a separate "HW4 rules" document | Same pattern as the §7.1 HW2 amendment (lifted the multi-agent prohibition for exactly what HW2 named). LlamaIndex, CrewAI, AutoGen, PydanticAI, Haystack, vector DBs and external code-indexers stay out of scope — a new dependency still needs a decision record, not just an opportunity (CLAUDE.md §4). |
| 54 | 2026-08-03 | tools/__init__.py, tools/text_tools.py | The conversion sweep is a **registry-assembly** change: `build_langchain_registry()` runs the whole `TOOL_FUNCTIONS` list through `to_langchain_tool` (#50) and exposes `LANGCHAIN_TOOLS`, and the second new tool (`diff_texts`) is registered by adding it to that list — **no tool body or signature is touched**. The one online test drives the real model to *select* the new tool through the compiled graph | Hand-wrapping each of the eight graph tools as a `@tool`/`StructuredTool` in its own module; making the online test assert only that the tool *runs* once named | All ten tools already convert cleanly through the single point (verified), so a per-file rewrite would only re-introduce the eight-places-to-drift risk decision #50 exists to remove — the sweep belongs at the one assembly point, not scattered. Invariant #25 then holds by construction across the whole registry: conversion reads only parameters the author wrote, and none of them is identity or scope (a test asserts no `author_id`/`impacted`/… field appears). The online test asserts *selection*, not just execution, because the framework-conversion bug §8 targets is a schema that never reaches the model — a test that hand-picks the tool would pass even then. `diff_texts` pairs with `apply_change` (show the change vs. land it) and is ungated because a diff is read-only (CLAUDE.md §5), the same reason `evaluate_expression` is. |
| 55 | 2026-08-02 | tests/_online.py, tests/test_loop_contract.py | The §8 online gate is **one shared fixture**, and it (a) treats a placeholder key as *no key*, (b) reads `.env` **without exporting it**, and (c) exports the real key only for the duration of the test that asked. Contract #9 gets its own suite, separate from `test_graph_agent.py` | A per-suite `skipif(not os.environ.get("OPENCODE_API_KEY"))`; a module-level `load_dotenv()`; folding the contract assertions into the graph-internals suite | Three failure modes, each found by hitting it. **(a)** `.env.example` ships `OPENCODE_API_KEY=sk-...` and a half-configured checkout copies it verbatim; that string is truthy, so a naive check *runs* the online test and reports the provider's 401 as a red test. Absent key is a **skip** — the suite has nothing to say about correctness when it cannot reach a model, and only a real failure should be red. **(b)** A module-level `load_dotenv()` in a shared helper exports the placeholder for the whole session and silently flipped `test_graph_agent.py`'s own skip decision, turning its online test red from an unrelated file — so the decision reads `.env` via `dotenv_values` and mutates nothing. **(c)** `agentlib.core` reads the key from `os.environ` at call time, so it must be exported *somewhere*; a fixture scopes that to one test and restores the previous value, which is the smallest window that still works. The contract suite is separate because it asserts a different thing: `test_graph_agent.py` tests the graph's internals (state schema, tool wrapping, routing), `test_loop_contract.py` tests only what crosses the boundary four other files depend on — signature keywords, return keys, branch tags, and `run_id`'s presence rule. If a later refactor drifts the shape, four callers break at once and the failure should name the contract, not surface as four unrelated bugs. |

### HW5 decisions

Phase A (contracts) is #56-#62; Phase B (the retrieval layer) is #63-#65. Phase C (retrieval
metrics) is #66-#67; #68+ are reserved for Phase D (generation metrics) so parallel branches do
not collide in this table.

| # | Date | Component | Decision | Rejected alternative | Why |
|---|------|-----------|----------|----------------------|-----|
| 56 | 2026-08-04 | repo-wide / CLAUDE.md §4, §7.1 | The §4 "vector databases, embedding services" ban is lifted for **`psycopg` + `pgvector` and OpenRouter's embeddings endpoint only**; §7.1's "retrieval over the graph" ban is lifted for what HW5 names | adding a purpose-built vector store (Chroma, Qdrant, Weaviate); lifting the dependency rule generally; leaving the ban in place and hand-rolling a pure-Python cosine index | Same narrow-amendment pattern as §7.1 (HW2) and §4 (HW4): name the pieces, lift nothing else. Postgres was chosen over a dedicated vector store because the corpus needs *filtering* as much as similarity — chunks carry `symbol_uid`, `kind` and `visibility`, and the visibility predicate (#24) has to be a `WHERE` clause on the same query as the vector scan, not a post-filter. A pure-Python index would have honoured the old rule but at ~600 chunks the honest reason to avoid a database was gone, and the rule was written for HW1's "understand the loop" constraint, not as a permanent architectural claim. What is *still* hand-written is the part being graded: BM25, RRF, the reranker, and all five rank metrics. Ragas, DeepEval, LlamaIndex, CrewAI, Haystack and graph databases stay out. |
| 57 | 2026-08-04 | overlay.db `node_summaries` | Node summaries — authored prose saying what each module/symbol is *for* — live in the **overlay**, keyed on `resolve_uid`, never in `knowledge_graph.json` | adding a `summary` field to the derived nodes, where the scanner already has the file open and could fill it in cheaply | This is #5 and #16 applied to a new kind of row, and the cost of getting it wrong is invisible until it is total: decision #16 lets any scan replace the derived layer *wholesale*, so a summary written into a node survives exactly until the next `scan_repository_structure` and then vanishes with no error. Summaries are the expensive artefact here — one model call each, and the thing retrieval actually searches — so losing them silently on a re-index would be the worst failure the store could have. Keyed on `symbol_uid` (#22) means a component that moves orphans its summary rather than dropping it, same as a decision. `(symbol_uid, symbol)` is UPSERTed rather than append-only, unlike `decisions`: a summary is a current description, not a historical claim, and keeping superseded wordings would fill the retrieval corpus with near-duplicates of itself — precisely the failure mode reranking handles worst. |
| 58 | 2026-08-04 | retrieval.bm25 | BM25 is **hand-written in Python** (Okapi, `k1=1.2`, `b=0.75`), not Postgres full-text ranking and not `rank_bm25` | `tsvector` + `ts_rank_cd`, which would put both retrieval arms in one SQL query; the `rank_bm25` package; ParadeDB/`pg_search` for real in-database BM25 | **Postgres FTS ranking is not BM25, and the gap is not cosmetic.** `ts_rank`/`ts_rank_cd` consult no corpus-wide document frequency at all — there is no IDF — and IDF is the entire mechanism by which the lexical arm beats dense retrieval on the queries it was added for: rare identifiers like `impact_scope`, `prune_graph_node`, `symbol_uid`. Without it, `agent` (in nearly every chunk of this corpus) and `pgvector` (in one) weigh the same. It also lacks `k1` saturation, which bites concretely here: `docs/TODO.md`'s ownership tables repeat the same names dozens of times, and a linear-in-tf ranker floats them to the top of half the queries. ParadeDB would give genuine BM25 but makes the parameters the extension's to justify rather than ours, on a corpus of ~600 chunks where all three options have identical latency. `overlay/memory.py::_tokens` already supplies the tokenizer. |
| 59 | 2026-08-04 | retrieval.search | `search()` returns the **retriever's** ranked order. Any lost-in-the-middle repacking is a separate function applied downstream, never inside the search path | packing for the context window inside `search()`, which is where it is most convenient | MRR and nDCG read rank position; hit rate, precision and recall do not. Feed a repacked list into all five and **two silently degrade while the other three look fine** — a discrepancy that reads as a retrieval regression and is actually an instrumentation bug. Keeping the reorder outside the seam means the metrics cannot be fed the wrong order by accident, rather than relying on the harness to remember. |
| 60 | 2026-08-04 | tools/retrieval_tools.py | `search_corpus` is a **tool the model chooses to call**, with a `rerank: bool` parameter, never a pipeline stage that runs before every request | retrieving on every turn and prepending the chunks; a fixed retrieve→generate chain | A fixed pre-retrieval step spends a search on every "hi" and cannot re-query when the first result set is thin. As a tool the model can skip it, or refine and call again — which already works, because `guards.detect_stall` (#10) stops an *identical* repeated call but lets a genuinely different query through, so re-querying needed no new machinery. `rerank` is deliberately both a real agent-facing control and the seam the eval harness toggles: the reranking-on/off comparison then measures the same code path the agent actually uses, rather than a test-only branch. Scope stays ambient (#25) — no `user_id` parameter — and decision chunks are filtered by the `visible_to` predicate inside the SQL query (#24). |
| 61 | 2026-08-04 | eval/ | Judged generation metrics are **hand-rolled** on `monitor/judge.py`'s pattern; the eval harness lives in `eval/`, separate from agent code | Ragas; DeepEval (what Session 11 runs, and the assignment's stated default) | Both would be substantial new dependencies under a §4 amendment that just named its pieces, and both score numerically, which contradicts #37 (grade on named values, never a 1-10 score) — adopting one would mean either amending #37 or carving an exception into the one place we already judge model output. We also already own a judge with a house style for exactly this. Owning the prompts matters for a graded requirement: judge bias (position, verbosity, self-preference, judge-model mismatch) has to be *addressed and described*, which is hard to do honestly about prompts you cannot see. |
| 62 | 2026-08-04 | store/, retrieval.store | The sqlite overlay **does not migrate** to Postgres. Postgres holds only the derived retrieval index and may be dropped and rebuilt at any time | moving `decisions`/`runs`/`silences` into the new database now that one is running | Recorded because it is the temptation this homework creates, not because anything proposed it: once a real database is in the compose file, consolidating looks free. It is not. #21 and #23 chose the split on "is the query the product" and on the derived/authored boundary, and neither reason changed — what changed is only that a *derived* index now needs a database too. Keeping the authored half in a diffable file under `store/` also preserves the property that the durable knowledge survives `docker compose down -v`. |
| 63 | 2026-08-04 | retrieval.cache | The embedding/rerank cache is **one sqlite file** of float32 vectors under `store/cache/`, gitignored — and it is kept for **reproducibility first, cost second** | one JSON file per vector (the first implementation: 962 files, 31 MB); no cache at all, re-embedding per run | The cost argument alone would not justify a cache: a full re-index is ~$0.002 and 41s. The reproducibility argument does, and it was **measured, not assumed** — `text-embedding-3-small` is *not* bit-deterministic. The same string re-embeds with up to ~1.2e-4 drift per component (asserted in `tests/test_retrieval_online.py`). Cosine similarity stays ~1.0 so rankings are stable in general, but two chunks closer than that margin can swap, and MRR/nDCG read position. So Part 2's tables are identical across runs *because the vectors come from the cache*, not because the endpoint is stable. One sqlite file rather than a directory: JSON stores a float as ~11 characters where `array('f')` uses 4 bytes (31 MB -> 7.7 MB), a thousand small files are slow to stat and noisy in every tree walk, and representing one derived artefact as 962 files invited the question of whether some should be committed. float32 loses ~1e-7, three orders of magnitude below the endpoint's own drift. |
| 64 | 2026-08-04 | retrieval.search | A `source=` filter that matches nothing returns an **empty result**; only an index with no visible chunks at all raises `IndexEmpty` | one "nothing came back" branch covering both | Found by the online tests. Collapsing them sends people to the wrong fix — `search_corpus(source="components")` before the summariser has run is a legitimately empty answer, and reporting it as `index_empty` reads as an outage and prompts a reindex that changes nothing. Same distinction decision #18 draws for the graph file: absent is a valid answer, corrupt is an error. |
| 65 | 2026-08-04 | retrieval.rerank | The reranker sorts candidates into three **named bands** (`answers`/`related`/`unrelated`) and defers to fused order within a band; an unjudged candidate keeps its fused position rather than being dropped | asking for a 0-10 relevance score per candidate; dropping candidates the model did not return a verdict for | #37's rule applied to a new judgment: a model asked for a number clusters everything at 7 and produces suspiciously smooth distributions, while three named buckets force a commitment a human can audit in the trace. Deferring to fused order inside a band means the reranker only ever *reorders across* bands, so it cannot destroy the fusion signal it was handed. Dropping unjudged candidates was rejected because a reranker that silently deletes results is strictly worse than one that declines to reorder them — and parse failures are the common case with a cheap model. |
| 66 | 2026-08-05 | agents.planner | The planner's seed is resolved **through `search_corpus`** (`source="components"`): retrieved cards are shown to the model as a candidate list, and if the model still names no seed the **top-ranked candidate is used** rather than failing the run | keeping the blind prompt and instead dumping the whole node list into it (the fix originally filed in HW4); making retrieval a hard import so the planner cannot run without Postgres | Closes the HW4-filed bug: `_propose_seed_and_steps` asked the live cheap model to name a component with *no list in front of it* and forbade inventing files, so it returned `{"seed":""}` and `run_planner` failed on a request it should have planned. Retrieval is the better fix the TODO (T14.13) named: it makes the model *pick* from a pre-narrowed, ranked list rather than recall a name (the graph is the router, #27), and the empty-seed fallback to the top hit means a retrievable request can no longer fail on a blank seed. `search_corpus` is imported lazily and any failure (no Postgres, no `psycopg`, empty index) degrades to the original blind prompt, so the offline suite and a no-database checkout keep working — retrieval improves the seed, it is not a new dependency of planning. Scope stays ambient (#25): the tool exposes no identity/scope arg to leak. The *second* filed planner defect (`_brace_slice` using `rfind("}")` not the matching brace) is deliberately **not** folded in here — it lands as its own change (`overlay/summarize.py` has the depth-scan to copy). |
| 67 | 2026-08-05 | eval/ (retrieval metrics) | The five rank metrics are scored with three fixed conventions: **empty-golden cases are `None` (undefined), not 0** and are excluded from every average and counted separately; **precision@k's denominator is what was returned** (`min(k, len)`), not a flat `k`; and every metric reads **`Hit.rank`**, never list position | scoring out-of-corpus cases as 0 (the intuitive default); flat-`k` precision (the textbook default); trusting `search()`'s list order without re-reading `rank` | Each convention is a specific wrong number avoided. (1) An out-of-corpus query has no relevant chunk, so precision/recall/MRR/nDCG have *no value*; scoring 0 punishes correct abstention and drags the mean down, so `None` + `mean()` that skips `None` keeps the average honest and the excluded count is reported. A third status, **`unresolved`** (anchors declared but none resolve against the live index — a stale golden), is excluded and counted the same way, so a re-chunk that moved the goldens surfaces as a number rather than as depressed recall. (2) "Fraction of *what you returned*" (the assignment's words) diverges from flat-`k` only when the retriever returned fewer than `k`, where flat-`k` penalises a short list for being short. (3) `search()` returns retriever order (#59), but `ranked_ids_from_hits` sorts on `Hit.rank` anyway so a repack, a `set`, or a dict round-trip upstream cannot silently feed MRR/nDCG the wrong order. Goldens are **content anchors resolved at load** (decision, symbol, heading), not chunk ids, because ids are `sha1(identity)[:16]` and move on every re-chunk (contract #10). |

### Demo-branch decisions

`#68-#69` stay reserved for HW5 Phase D (generation metrics); the `final-demo` branch takes
**#70-#79** so the two do not collide in this table.

| # | Date | Component | Decision | Rejected alternative | Why |
|---|------|-----------|----------|----------------------|-----|
| 70 | 2026-08-05 | service.py | `search_corpus` is added to `_READ_TOOLS`, so the **channel path** offers it too — the same tool `main.py` has offered since Phase B | leaving the channel registry as the four exact lookups; adding it only for allowlisted users | The omission was a gap, not a posture: §7.3 makes `search_corpus` "a tool the existing agent chooses to call", and the channel is where the agent is most often asked. The four exact lookups all require the asker to *already name a component*, which is how a maintainer asks and is not how anyone else does — the observed channel runs are questions like "dont you see my previous messages?" answered with zero tool calls, because nothing offered could take an unnamed target. Safe on the anonymous path for the reason the others are: `retrieval.store._visibility_clause` mirrors `overlay.db.visible_to` against the ambient `current_user()` inside the SQL, on **both** the dense and the lexical arm (#24, #25) — the model is never handed rows it must be told to ignore. Read-only, so ungated (§5). No new import risk: `psycopg` is imported lazily inside `retrieval.store.connect`, so a checkout without Postgres degrades to the `index_unavailable` branch (#64) rather than failing `service.py` at import. |
| 71 | 2026-08-05 | app/ | The demo surface is a **read-only observer** over `app/logs/service.log` and `store/runs/runs.jsonl`; it imports no agent code, opens no store, and adds no dependency | instrumenting the agent to emit demo events; a websocket/callback hook inside `run_agent`; a web framework | A demo that changes the thing it demonstrates is not evidence. Both files already exist for their own reasons — stdout because `service.py` prints its stages, `runs.jsonl` because the monitor grades it out of band (#40) — so the panel is assembled from what the system already produces and deleting `app/` leaves behaviour byte-identical. Both sources are needed and neither suffices: stdout is live but coarse, the run record is exact but only lands when the turn ends. The run record is also replayed **out of file order** — a `/change` run shares one `RunLog`, so the executor's tool calls sit in `steps` while the planner's envelope sits later in `envelopes`, and replaying the file's order shows the executor acting before the plan exists. `http.server` over a framework for the same reason as #58: the stdlib already covers it, and one button does not justify a dependency (§4). |
| 72 | 2026-08-05 | agents.planner | `_retrieve_seed_candidates` **prints a `[retrieval]` line** naming the query, the passage count, whether the reranker ran, and the top candidates — and the panel renders it in the tool lane, with the detail marking it code-owned | leaving it untraced; or having `app/` synthesise a retrieval row from the presence of a plan envelope | The call is real retrieval — the same `search_corpus`, same args, same results — but Python makes it rather than the model choosing it (#66), so it reaches no `run_agent` trace and, until now, nowhere else: the one retrieval the change pipeline actually *depends* on was the only one nobody could see. That is #34's rule applied to a step it had not yet been applied to — the impact walk prints its seed and cap for the same reason — because otherwise "the retriever surfaced nothing" and "the retriever was never asked" are indistinguishable when a plan comes out wrong. The panel showing it in the tool lane is accurate rather than dressed up: every field on the row is what was really called and really returned, and the `[code-owned: planner seed retrieval]` marker in the detail is there so the row does not imply the *model* picked it, which is the one thing that would be false. Synthesising the row app-side was rejected outright — the app is an observer (#71), and an observer that invents events it did not witness is not one. Logging only; `run_planner`'s contract, return shape and behaviour are unchanged, and the two private helpers gained a keyword-only `verbose` with a default so no caller or test changed. |
| 73 | 2026-08-05 | app.theme | The demo page's colours and copy live in **`app/theme.py`**, substituted into `static/index.html` per request; the HTML holds `{{TOKEN}}` placeholders and no literal values | keeping the colours in the stylesheet where they belong; teaching `repo_scan` to index `.html` | `repo_scan` indexes `.py` and `.md` only, so a colour in a stylesheet is not a graph node, is not in the retrieval corpus, and can be in no plan's impact set — `apply_change` refuses it with `outside_impact_set` however well the change was planned. Moving the value into a module is not a workaround for that rule, it *is* the rule: the graph bounds what the system can reason about, and something it should be able to change has to live where it can see it. Indexing HTML was rejected as the bigger change — it would put every template in the corpus and in impact sets, for one demo. Substituting per request (with `importlib.reload`) means an agent edit lands on the next browser reload with no restart, which is what makes the `/change` half of the demo visible. An unknown token stays on the page as `{{WHATEVER}}` rather than rendering blank — a loud failure over a silent one. |
| 74 | 2026-08-05 | agents.planner | `_brace_slice` extracts the first **balanced** `{...}` by depth scan, tracking strings | the `find("{")`/`rfind("}")` span it replaced | The defect #66 filed and deferred to its own change. `rfind` spans to the last brace *anywhere* in the reply, so any second object makes the slice unparseable — and the live cheap model does exactly that: asked to plan "change the button color to green" it emits the correct object **twice, concatenated**, and the planner failed with "could not parse a seed/steps plan" on a request it had planned correctly moments earlier. Reproducible, not flaky. Ported from `overlay/summarize.py::_brace_slice`, which hit the same failure first at roughly one cheap-model reply in four. |
| 75 | 2026-08-05 | agents.executor | An executor run that writes **no file** returns `blocked`, never `ok` | leaving the fall-through `ok`; reading the answer's prose for "blocked"/"cannot" | The executor exists to carry out a plan, so a run that changes nothing either refused or gave up, and `ok` is the one status that tells the caller neither — one field away from concluding the change went in. Found on the brief's own escalate rule: "a recorded decision in `constraints` forbids the change outright" -> `blocked`. That refusal arrives as an **answer with no tool call**, which every existing branch misses (they key on `declined`, on loop faults, and on confinement errors), so the system's clearest success — refusing a change because a recorded decision forbids it — was reporting itself as `ok`. Decided on the trace, never on prose (#29): "wrote no files" is a fact, "said it was blocked" is a sentence. Which refusal it was stays in `notes`. |
| 76 | 2026-08-05 | main.py, service.py, orchestrator.py | The three CLI entry points default to **`--model strong`**; `--model cheap` stays available | keeping `cheap` as the default and passing `--model strong` when it matters | The cheap model's failure modes on *this* workload are parse-level, not quality-level, and they surface as system faults rather than as weaker answers: `{"seed":""}` with no node list in front of it (#66) and the plan object emitted twice, concatenated (#74). Both were fixed, and both were found only because the default made them the common path — a default that turns model limitations into "the planner failed" is the wrong one to hand a new session. The **library** defaults are deliberately untouched: `agentlib.core.call`, `loop`, `graph` and `run_planner` still default to `CHEAP`, so tests and any programmatic caller keep the model they were written against and only the interactive paths move. `retrieval.rerank` stays `CHEAP` on purpose — #65's three named bands are designed for a cheap model, and the eval harness measures that exact path, so changing it would move Part 2's numbers. Cost is the real trade and it is ~25x on input and ~24x on output (`gpt-5.5` at $5.00/$30.00 per 1M vs `gpt-5.4-nano` at $0.20/$1.25); acceptable for interactive use at this repo's volume, which is why the flag was kept rather than removed. |
| 77 | 2026-08-05 | app.reset_demo, app.server | Reset undoes **only** the demo's two marks (`app/theme.py` via `git checkout`, decisions `WHERE symbol_uid = 'Module:app.theme'`) and rebuilds nothing; the knowledge panel renders chunk text **fetched from Postgres**, never rebuilt page-side | a reset that drops and rebuilds the graph, summaries and index "to be safe"; rendering the card and calling it the chunk | Two separate refusals to blur the derived/authored line. On reset: the tempting version re-scans and re-indexes, which would be slow, would spend embedding calls, and would quietly make the authored layer look regenerable — it is not (§6). Nothing about a colour constant changes which modules exist or what they are *for*, so graph nodes, summary cards and their chunks all stay valid and `app.theme` stays searchable across a reset; `content_sha` is the only value that moves during a run and restoring the file restores it. The `DELETE` is scoped to one `symbol_uid` because a reset script is precisely where a too-wide delete does permanent damage. On the panel: showing the overlay card and *calling* it the chunk would be a paraphrase presented as the real thing — the join from authored card to indexed passage is what the panel exists to make visible, so the passage is read back from the index by `symbol_uid` and is byte-identical to what `search_corpus` returns. The endpoint refuses any uid outside `Module:app*`: the overlay holds rows this page is not the right surface for. |
| 78 | 2026-08-05 | agents.executor | A plan with **no steps** returns `needs_input`, not `ok`; `tests/test_orchestration.py::test_an_empty_step_list_is_ok_not_a_failure` is amended and renamed accordingly | leaving it `ok` and keeping the test as written | The original test's point — an empty step list is **not a fault** — is right and is still asserted, because `needs_input` is the ask-a-human branch, not the failure branch. What it got wrong is that `ok` was also unavailable: the executor's status propagates to the orchestrator's final status, so a human who asked for a change was told `status: ok` for a run that touched no file. That is the identical wrong signal #75 removed from the tail of the same function, one branch earlier. Seen live, not theorised: the model names a valid seed but returns steps whose `path` is empty, `_clean_steps` drops them, and the plan arrives stepless. `needs_input` over `blocked`/`failed` because nothing forbade the change and nothing broke — the plan is merely unusable, and the fix is a human naming the file. **An existing test's assertion was changed**, which is normally a smell; recorded here so the amendment is visible rather than inferred from a diff. |
| 79 | 2026-08-05 | app.server, app/static | The trace panel moves to a **full-height right column** with two-line rows, and every row carries an **`actor`** (`system`/`user`/`agent`/`planner`/`executor`/`gate`) alongside its `kind` | keeping the bottom strip; deriving the actor from the message text | Two independent fixes. Layout: rows are short and numerous, so height is the scarce axis — a right column shows roughly three times the lines a bottom third did without shrinking the meme; below 900px it falls back to stacked rather than squeezing both. Attribution: `kind` answers *what happened* and nothing answered *who did it*, which is the one thing this system most needs to show, since it is a single read-only agent on the question path and a planner **plus** an executor on the change path — "a tool was called" is far less useful than "the EXECUTOR called a tool". Derived structurally, never guessed: stdout lines carry their stage in the prefix `service.py` already prints, and a run record's `steps` belong to the executor when `run.agent == "orchestrator"` and to the channel agent otherwise, because the planner issues no `run_agent` tool calls at all (#66). Actor drives the badge and the row's left rule; `kind` still tints the message. Fixing this surfaced two panel bugs: the generic `[telegram]` pattern shadowed the more specific inbound-message pattern (first match wins), and `already_showed_user` compared for equality against a line `service.py` prints `[:80]!r` — so every message rendered twice, which read as a redundant panel rather than as a broken comparison. |

---

## 6. Known gaps / deferred to later homeworks

- Orchestrator + Architecture/Discussion agent split (multi-agent, Session 5)
- ELI5 agent with Mermaid output
- ~~Retrieval over the graph (Session 10) — currently exact lookup only~~ — **closed by HW5**
  (decisions #56-#62): hybrid dense+BM25 retrieval over authored node summaries, behind
  `search_corpus`. The exact lookups (`query_component_graph`, `retrieve_decisions`) remain and
  are still the right call once a name is known — retrieval finds the name, the joins answer
  about it.
- Evaluation harness: coupling drift, decision consistency, rework rate, context cost (Session 11)
  — HW5 adds retrieval and generation evaluation under `eval/`; the *agent-level* metrics in this
  line are still open.

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

> **Update (HW2, 2026-07-26).** The first of those three open questions is now answered: the
> overlay lives in **SQLite at `store/radf.db`** (decision #21), and `symbol_uid` is a real key
> rather than a documented intention (decision #22). The structural half was deliberately left
> in JSON — see decision #21 for why building it out would have made this migration harder, not
> easier. `verify_graph_integrity(scope="all")` already performs the cross-store join, so the
> third question is answered too: it checks overlay uids against structural nodes, and will
> point at GitNexus's uids unchanged.

---

## 7. State layers (HW2)

> Durable principle: **separate your state layers.** One store for every layer is the
> conflation the whole homework is about.

| Layer | Tier | Where it lives | Written by |
|---|---|---|---|
| Conversation turns | short-term | the `messages` list inside one `run_agent` call | the loop |
| Task state | short-term | `run_scratch` (per `run_id`, append-only) | planner / executor |
| Operating rules | long-term | `rules/OPERATING_RULES.md`, `rules/modules/*.md`, `agents/executor_brief.md` | a human, by hand |
| Durable knowledge — decisions | long-term | `store/radf.db` → `decisions` | `append_decision_record` |
| Durable knowledge — free-form | long-term | `store/memory.json` | `save_memory` |
| Observation | after the fact | `store/runs/runs.jsonl` | `agentlib.runlog` |

Structure (`store/knowledge_graph.json`) is not a state layer. It is **derived** and belongs to
whatever indexes the repo — today an `ast` walk, later GitNexus.

### 7.1 Push and pull

`agentlib/context.py` fills context in both directions, and which direction a source uses is a
design decision rather than a detail.

| | Push | Pull |
|---|---|---|
| Who chooses | the code, before the call | the model, mid-run |
| Cost | tokens on every run | a round trip, wasted on a wrong guess |
| In the trace | invisible (so it is logged explicitly) | visible as a tool call |
| On failure | *we* assembled the wrong thing | *it* never went looking |
| Used for | operating rules, module rules bound by the impact set, the session header | decisions, memory, the component graph, per-module rule files |

Ordering inside `instructions` is static-first (the shared prefix stays byte-identical and
cacheable), per-user last (where instructions are best obeyed). The user's actual request is the
**last** `input[]` item, after any quoted data.

### 7.2 Facts, rules, and who decides what

- A **fact** is information. It is saved with a cue, resurfaces on that cue, and the *model*
  decides what to do with it.
- A **rule** already says what to do. The model does not interpret it — it only has to be in
  force at the right time, and mostly the model does not decide that either (decision #27).

### 7.3 Visibility

Two orthogonal axes on every authored row:

|  | `symbol_uid IS NULL` (repo-wide) | `symbol_uid = 'Module:tools.decisions'` |
|---|---|---|
| `visibility = 'team'` | "raw stdlib only, no frameworks" | "tools/* return dicts, never raise" |
| `visibility = 'user:berat'` | "run the tests before proposing a diff" | "keep the `_`-private helpers here" |

Team constrains; personal decorates. On conflict team wins, and the conflict is *recorded* —
that is the contradiction the monitor is meant to find (T7.4), not something to resolve silently.

### 7.4 New components

| Component | Owns | Depends on |
|---|---|---|
| `overlay/db.py` | SQLite schema; `decisions`, `runs`, `run_scratch`, `scratch_reads`; the `visible_to` filter | `overlay.uid` |
| `overlay/uid.py` | `resolve_uid` — the join key, and the seam for the GitNexus swap | — |
| `overlay/memory.py` | free-form memory; cue+recency ranking; `proposed`/`accepted` promotion | — |
| `agentlib/session.py` | `SessionKey`, `session_scope`, `impact_scope` — ambient identity and write scope | — |
| `agentlib/context.py` | context assembly, push/pull split, quoting of untrusted text | `overlay`, `agentlib.session` |
| `agentlib/runlog.py` | the run record the monitor grades | — |
| `agents/envelope.py` | `AgentResult`, the plan dict, `validate_plan` — the frozen inter-agent contract | — |
| `agents/planner.py` | transitive-`imported_by` impact set (capped) + constraints → a plan; one model call for the seed, the walk is pure Python *(done; Alejandro, T6.2)* | `agents.envelope`, graph + decision tools |
| `agents/executor.py` | carries out a plan; narrow toolset; brief pushed every run | `agents.envelope`, `agentlib.context` |
| `orchestrator.py` | plain-Python routing on envelope fields; owns the single run record | both agents, `overlay.db` |
| `tools/read_source.py` | confined file read for the executor | `tools.apply_change` (denylist) |
| `tools/apply_change.py` | the gated file write; path + impact-set confinement, before/after hash *(done; Alejandro, T7.1)* | `agentlib.session`, `overlay.uid` |
| `monitor/judge.py` | LLM-as-judge over run logs — two named axes; code drops unbacked verdicts and splits ignored-rule from never-assembled *(done; Dias, T7.3)* | `agentlib.runlog`, `agentlib.core` |
| `monitor/rubric.md` | hand-editable rubric pushed to the judge; where the minor/serious line falls *(done; Dias, T7.3b)* | — |
| `inspect_store.py` | read-only CLI over all four stores; `trace <run_id>` renders the cross-store causal chain | `overlay`, `agentlib.runlog` |

**Contract — `agents/planner.py` (Alejandro, T6.2).**
```
run_planner(request, *, component_hint="", max_hops=2, model=None,
            max_steps=8, verbose=True, run_log=None) -> AgentResult
  ok          plan dict, open_questions == []      (executor may act alone)
  needs_input component absent from the graph, or two decisions conflict
  failed      graph unreadable, or the model's seed/step proposal is unusable
```
The plan carries the frozen keys plus a non-frozen `impact_max_hops`, so the
depth cap is visible in the plan, the scratch and the run log. The model is
called exactly once (seed + steps from free-form text); the impact walk and its
cap are pure Python (decision #34), and conflicting decisions become open
questions (decision #35). `component_hint` skips the model entirely.

**Contract — `monitor/judge.py` (Dias, T7.3).**
```
judge_run(run, *, model=STRONG, rubric=None) -> Verdict
  Verdict{run_id, prompt_adherence, grounding, violations[], assembler_gaps[],
          dropped[], gradeable, notes, model}
  prompt_adherence  strictly_adheres | minor_violation | serious_violation  (or "ungraded")
  grounding         grounded | partially_grounded | ungrounded              (or "ungraded")
```
The model proposes; the code decides (decisions #37–#40). `violations` are
in-context and rationale-backed only; a cited rule absent from
`assembled.instructions` lands in `assembler_gaps`; a rationale-less accusation
lands in `dropped`. Unparseable/truncated judge output → `gradeable=False`, never
a guessed grade. `judge_runs` / `report` / `problems` grade and summarise the
whole log; `python -m monitor.judge` is the "separate job on its own clock".
Grades from the frozen run-log record (§7.4, `agentlib/runlog.py`) only — no
tools, no store. `rubric.md` is the hand-editable line-drawing, pushed to the
judge; `demos/demo_monitor_finding.py` seeds the R5-vs-personal-rule contradiction
(T7.4) and shows the monitor report it with expected-vs-observed.

**Contract — `tools/apply_change.py` (Alejandro, T7.1).**
```
apply_change(path, new_content, intent="edit"|"create") -> dict
  ok    {"path","written":true,"intent","before_sha","after_sha","bytes"}
  err   path_outside_scope | outside_impact_set | no_plan | file_missing |
        file_exists | invalid_args   (RETURNED, never raised)
```
Path confinement (resolve-then-compare + `DENYLIST`) and impact-set confinement
(against the ambient `current_impact_set()`, never a parameter) are both in the
tool, not the prompt (decision #36). A refused write leaves the file
byte-identical. In `GATED`, so an approved write still passes the human gate.

---

## 8. The channel (HW3)

HW1 and HW2 both began at a prompt somebody typed, which means a human had already decided
there was something worth doing. The failures worth catching do not arrive that way: a
decision goes stale when someone moves the file it points at, and a run misbehaves at 3am.
HW3 adds the surface, the triggers, and the one outcome the earlier homeworks could not
express — deciding not to answer.

### 8.1 Components

| Component | Owns | Depends on |
|---|---|---|
| `channel/base.py` | `InboundEvent`, `OutboundReply`, the `Channel` protocol — one inbound shape for every source | — |
| `channel/identity.py` | external id → `SessionKey`; the allowlist, the anonymous identity, `BOT_AUTHOR_ID` | `agentlib.session` |
| `channel/queue.py` | admission policy + the single worker | `channel.base` |
| `channel/silence.py` | `SilenceDecision`, the closed reason-code set, and `evaluate_silence` — the private-decision leak guard, a pure comparison of two visibility views *(body done; Dias, T11.2)* | `channel.base`, `agentlib.session` |
| `channel/telegram.py` | the Telegram transport; long-poll, offset persistence, backoff | `channel.base` |
| `agentlib/approval.py` | `ChannelGate` — the approval gate with the answer arriving over the channel | — |
| `service.py` | the long-running process: poll → identity → queue → worker → reply; the two narrow registries | all of the above, `orchestrator`, `overlay` |
| `triggers/__init__.py` | the shared event shape for Phases 10 and 11 | `channel.base` |
| `triggers/webhook.py`, `triggers/orphan_watch.py` | GitHub events → orphaned-decision detection *(Alejandro, Phase 10)* | `channel.base`, `tools.decisions` (read-only) |
| `triggers/heartbeat.py` | the monitor's real clock — threshold of unjudged runs + persisted watermark; posts `problems()` only; clean pass records `heartbeat_clean` *(done; Dias, T11.1)* | `monitor.judge`, `overlay.db` |
| `agents/admin.py`, `rules/ADMIN_BOUNDARY.md` | the privileged path — registry narrow by explicit list, gated on admin id **and** confirmation, write scope granted per run *(done; Dias, T11.3)* | `channel.identity`, `agentlib.session` |

### 8.2 Contracts

**`channel/base.py` — the one inbound shape.**
```
InboundEvent(source, thread_key, text, external_user_id, payload, ts, dedupe_key)
  source ∈ telegram | github | heartbeat
  external_user_id  None for machine sources — they inherit nobody's session
  dedupe_key        set ⇒ COALESCABLE. Never set on a human message.
  .interactive      True iff a human is waiting
OutboundReply(thread_key, text, silent) · OutboundReply.quiet(thread_key)
```
Everything on an event is untrusted, including fields that look structural. `external_user_id`
is the only field with any standing and it is used as a lookup key, never as an identity.

**`channel/identity.py` — who the runtime says is asking.**
```
resolve(event) -> Identity{session, known, is_admin, external_id, source}
  .can_write   == known.  An unmapped sender reads team rows and writes nothing.
BOT_AUTHOR_ID = "bot:radf"
allowlist()  <- RADF_CHANNEL_USERS   "telegram:<id>=<radf_user>,..."
admins()     <- RADF_CHANNEL_ADMINS
```
An unmapped platform id resolves to `SessionKey(user_id="")`, not to itself, so the first
stranger to message the bot cannot pick their own visibility scope.

**`channel/queue.py` — admission, and only admission.**
```
WorkQueue(gate=None, maxsize=256)
  .submit(event, *, user_id) -> Admission{disposition, reason, accepted}
    disposition ∈ queued | coalesced | dropped | rejected | gate_reply
  .take(timeout) · .close() · .pending_sources()
Worker(queue, handler, on_error=None)
  .start() / .run() / .stop() / .drain()   drain() = same policy, no thread
```

**`channel/silence.py` — the recorded non-answer.**
```
SilenceDecision{silent, reason_code, evidence, visibility}
  reason_code ∈ heartbeat_clean | private_decision_leak |
                no_decisions_touched | injection_attempt
evaluate_silence(event, session, candidates) -> SilenceDecision   # T11.2, Dias
```
`candidates` is the **unfiltered** decision set, from `overlay.db.decisions_across_scopes` —
the only cross-scope read in the system. Those rows die on that stack frame: the function
returns a decision, never text. `evidence` records what was *checked*, never what was withheld.

**`agentlib/approval.py` — the gate, off stdin.**
```
ChannelGate(send, timeout=180)
  .callback_for(user_id, thread_key) -> approve(name, args) -> bool   # run_agent's shape
  .submit_answer(*, user_id, thread_key, text) -> bool  # True == consumed, not a request
  .pending -> PendingApproval | None    .cancel()  # shutdown declines
```
Only the requester's answer counts, in the thread they were asked in. Only `AFFIRMATIVE`
approves; everything else, including a timeout and including shutdown, declines.

**`service.py` — the process.**
```
python service.py                 # listen on Telegram
python service.py --dry-run       # queue policy, no network, no model
build_channel_registry(can_write) -> (schemas, registry)
  read-only : search_corpus, query_component_graph, retrieve_decisions,
              retrieve_memory, verify_graph_integrity
  +can_write: append_decision_record, save_memory
```
Both registries are built from explicit lists, never by filtering the full registry — a
filter is one bug away from being a full registry. No `GATED` tool appears in either; writes
go through `/change`, which runs the HW2 orchestrator with the gate wired to the channel.

`search_corpus` joined the read-only list on the demo branch (decision #70) and leads it, as
in `tools/__init__.py`: it is the only one of the four that takes a question phrased the way
people phrase them in a chat window and returns something to look up. Visibility is still a
`WHERE` clause on both retrieval arms (#24), so the anonymous path is unchanged, and it needs
no gate because it is read-only.

### 8.3 `store/radf.db` — the `silences` table

```sql
silences(silence_id PK, run_id, trigger, reason_code, evidence, visibility, ts)
```
Read with `query_silences(conn, user_id, reason_code=None, limit=50)`, filtered through the
same `visible_to` fragment as decisions. It carries its own `visibility` because the most
important silence — the private-decision leak guard — must be readable by the *owner* of the
withheld decision and by nobody else. A silence log everyone could read would announce
exactly what the silence was protecting.

Inspect with `python inspect_store.py silences [--user <id>]`.
