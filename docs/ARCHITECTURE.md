# ARCHITECTURE.md

> This file is the durable-knowledge layer for the repo. Every merged PR that adds or changes a
> component updates it. Sections marked `<!-- OWNER: ... -->` are filled in by that owner as
> their work lands. Keep entries terse; this is read instead of the code.
>
> Last updated: 2026-07-26 — HW2 state layers, overlay, and the two-agent pipeline (Berat);
> planner + gated `apply_change` implemented, branch `hw2/alejandro/planner-and-write` (Alejandro)

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

## 2. Current state (HW3)

HW3 puts a surface in front of the HW2 pipeline. Everything below the dashed line is
unchanged; what is new is that something other than a person at a terminal can start a run,
and that a run can correctly end in saying nothing.

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

**Four stores, four jobs:**

```
store/knowledge_graph.json   derived     nodes · edges          any scan regenerates it
store/radf.db                authored    decisions · runs ·     no scan may touch it
                                         run_scratch · silences
store/memory.json            authored    free-form facts+rules  cue-retrieved
rules/*.md                   authored    operating rules        edited by hand, pushed every run
```

The first is a **deliberate stand-in** for GitNexus and is not being built out (§6.1,
decision #21). The other three are ours and are the durable half.

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
- **Status:** **done** (Phase 0)

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

### `tools/__init__.py` — registry + schema assembly
<!-- OWNER: Berat -->
- **Owns:** `build_registry() -> (schemas, registry)`, module-level `SCHEMAS` / `REGISTRY`.
  Imports every tool stub so the loop runs end-to-end from day one.
- **Status:** **done** (Phase 0)

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
```
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
| 2 | | agentlib | Raw Python loop, no framework | LangGraph | HW1 constraint; framework refactor is Session 9 |
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
| `channel/silence.py` | `SilenceDecision`, the closed reason-code set, the `evaluate_silence` **contract** *(body: Dias, T11.2)* | `channel.base`, `agentlib.session` |
| `channel/telegram.py` | the Telegram transport; long-poll, offset persistence, backoff | `channel.base` |
| `agentlib/approval.py` | `ChannelGate` — the approval gate with the answer arriving over the channel | — |
| `service.py` | the long-running process: poll → identity → queue → worker → reply; the two narrow registries | all of the above, `orchestrator`, `overlay` |
| `triggers/__init__.py` | the shared event shape for Phases 10 and 11 | `channel.base` |
| `triggers/webhook.py`, `triggers/orphan_watch.py` | GitHub events → orphaned-decision detection *(Alejandro, Phase 10)* | `channel.base`, `tools.decisions` (read-only) |
| `triggers/heartbeat.py` | the monitor's real clock *(Dias, Phase 11)* | `monitor.judge`, `overlay.db` |
| `agents/admin.py`, `rules/ADMIN_BOUNDARY.md` | the privileged path and its written boundary *(Dias, T11.3)* | `channel.identity` |

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
  read-only : query_component_graph, retrieve_decisions, retrieve_memory,
              verify_graph_integrity
  +can_write: append_decision_record, save_memory
split_component_hint(request) -> (component_hint, request)
  "/change project/app.py — centre the title"  ->  pins the planner's seed
  "/change centre the title"                   ->  seed proposed by the model
```
`/change` accepts an optional **leading component**, passed through as
`orchestrator.run_change_request(component_hint=...)` — the channel's version of
the `--component` flag the CLI has always had. It is an affordance, not a new
capability: it pins a value the caller already knew, and a pinned seed *narrows*
the impact set rather than widening it, so it cannot authorise a write the model
would otherwise have been refused. It exists because the seed decides the impact
set and the impact set decides what may be written, which made it the most
consequential value in the plan and — via `_propose_seed_and_steps`, capped at
400 output tokens with reasoning competing for the budget — the least reliable
one: roughly one request in five failed before a single decision was read.
`_looks_like_component` is deliberately narrow, because a false positive plans
against a component the user never named, which is worse than not pinning at all.
Both registries are built from explicit lists, never by filtering the full registry — a
filter is one bug away from being a full registry. No `GATED` tool appears in either; writes
go through `/change`, which runs the HW2 orchestrator with the gate wired to the channel.

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
