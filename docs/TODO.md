# TODO.md — HW1

**Read this file before any implementation.** If a task is not here, it is not in scope.
Every file has exactly one owner. Stubs are contracts — do not fill in a stub you do not own.

Branch: `hw1/<owner>/<short-task>` · PR into `main` · no direct pushes.

---

## Ownership map

| Area | Files | Owner |
|---|---|---|
| LLM runtime wrapper | `agentlib/core.py` | **Berat Furkan Kocak** |
| Schema derivation | `agentlib/schemas.py` | **Berat Furkan Kocak** |
| Guardrails + gate policy | `agentlib/guards.py` | **Berat Furkan Kocak** |
| Agent loop | `agentlib/loop.py` | **Berat Furkan Kocak** |
| Registry assembly + CLI | `tools/__init__.py`, `main.py` | **Berat Furkan Kocak** |
| Repo docs | `CLAUDE.md`, `ARCHITECTURE.md`, `TODO.md`, `README.md` | **Berat Furkan Kocak** |
| Repo → graph tool | `tools/repo_scan.py` | **Alejandro Ramírez Trueba** |
| Graph query tool | `tools/graph_query.py` | **Alejandro Ramírez Trueba** |
| Graph data contract | `store/` schema, seed fixture | **Alejandro Ramírez Trueba** |
| Decision-log tool | `tools/decisions.py` | **Dias Sarkytbaev** |
| Integrity check (error branch) | `tools/decisions.py::verify_graph_integrity` | **Dias Sarkytbaev** |
| Destructive graph tool (gated) | `tools/graph_write.py` | **Dias Sarkytbaev** |
| Smoke tests | `tests/smoke_hw1.py` | **Dias Sarkytbaev** |

**Shared, changed only by agreement:** the stub signatures in `tools/*.py`, the graph JSON
schema in `ARCHITECTURE.md` §4, the `Result` fields in `agentlib/core.py`.

---

## Homework requirement → owner (grading traceability)

| HW1 requirement | Where it is satisfied | Owner |
|---|---|---|
| `agentlib.core` built from the notebooks as spec | `agentlib/core.py` | Berat |
| ≥2 self-designed tools, action-shaped names, when/when-not descriptions, ≥1 constrained param | `tools/repo_scan.py`, `tools/graph_query.py`, `tools/decisions.py`, `tools/graph_write.py` | Alejandro + Dias |
| observe → reason → act → verify loop + explicit stopping condition(s) | `agentlib/loop.py` | Berat |
| one human approval gate on an irreversible action | gate in `loop.py`, gated tool `prune_graph_node` | Berat (mechanism) + Dias (tool) |
| one tool error reaching the loop as its own branch | `verify_graph_integrity` → error branch in `loop.py` | Dias (check) + Berat (branch) |

---

## Phase 0 — Foundation (Berat, blocking; do first)

- [x] **T0.1** `agentlib/core.py` — Zen wrapper. OpenAI SDK, base URL `https://opencode.ai/zen/v1`,
      key from `.env` as `OPENCODE_API_KEY` (never hard-coded). `call(...)` accepting
      `prompt | messages`, `system`, `model`, `tools`, `max_output_tokens`.
- [x] **T0.2** `Result` dataclass: `.text`, `.tool_calls`, `.output_items`, `.status`,
      `.stop_reason`, `.truncated`, `.usage`. `truncated` is a first-class flag —
      "returned text" ≠ "finished".
- [~] **T0.3** `CHEAP` / `STRONG` model ids + `MODELS`; `estimate_cost(usage)` (input tokens
      include cached at cached rate; output tokens include reasoning at output rate).
      Structure + `estimate_cost` **done**; model ids/prices are env-overridable placeholders.
      _Still blocked on: exact Zen model ids + per-token prices from Slack._
- [x] **T0.4** `agentlib/schemas.py` — `schema_for(fn)` from signature + annotations + docstring.
- [x] **T0.5** `agentlib/guards.py` — `validate_args`, truncation check, stall detection,
      `GATED` set, approval-policy helper.
- [x] **T0.6** `agentlib/loop.py` — `run_agent(...)` with the gate hook, the error branch hook,
      and a `trace`. Stopping conditions: `answered`, `max_steps`, `stalled`, `declined` (+ `truncated`).
- [x] **T0.7** `tools/__init__.py` — registry + schema assembly, importing every stub so the
      system runs end-to-end against stubs from day one.
- [x] **T0.8** **Stub contracts** written into `tools/repo_scan.py`, `tools/graph_query.py`,
      `tools/decisions.py`, `tools/graph_write.py`: real signatures, real docstrings,
      real return shapes, `raise NotImplementedError`.
- [x] **T0.9** `main.py` — CLI, `.env` load, `input()`-based approval callback.
- [x] **T0.10** `CLAUDE.md`, `ARCHITECTURE.md` skeleton, `TODO.md`, `README.md` skeleton;
      `guidance/` folder with both Session 3 notebooks; `.env.example`; `.gitignore`;
      `requirements.txt`.
- [~] **T0.11** PR template **done** (`.github/pull_request_template.md`); branch protection on
      `main` is a GitHub server-side setting — apply manually in repo settings.

**Definition of done for Phase 0:** `python main.py "list the components of this repo"`
runs the full loop and fails only with `NotImplementedError` from the stubs.

---

## Phase 1 — Read path (Alejandro)

Owns the two read-side tools and the graph data contract.

- [ ] **T1.1** `scan_repository_structure(root: str, max_depth: int, kind: Literal["python","markdown","any"])`
      — walk the repo, extract modules and their imports (`ast` for Python), write nodes +
      edges to `store/knowledge_graph.json`. Real side effect: touches a file.
- [ ] **T1.2** `query_component_graph(component: str, relation: Literal["imports","imported_by","neighbors","all"])`
      — read-only lookup over the graph. Reversible → ungated.
- [ ] **T1.3** Constrain both schemas beyond what `schema_for` derives: `enum` on `kind` and
      `relation`, `required` on `root`/`component`, integer bound on `max_depth`.
- [ ] **T1.4** Descriptions must say **when not** to call — e.g. "do not call
      `scan_repository_structure` to answer a question about a component that is already in
      the graph; use `query_component_graph` instead." (See Part A, A4: the description is
      what decides tool selection, not list position.)
- [ ] **T1.5** Finalise the graph JSON schema in `ARCHITECTURE.md` §4 + commit a small seed
      fixture for tests.
- [ ] **T1.6** Update `ARCHITECTURE.md`: both component entries, the data contract, and any
      decision records (why `ast` over regex, how re-scans merge vs. overwrite).

**Depends on:** T0.4, T0.7, T0.8. **Blocks:** T2.1, T2.2, T3.x.

---

## Phase 2 — Write path, safety & tests (Dias)

Owns the decision log, the error branch, the one gated tool, and the smoke tests.

- [ ] **T2.1** `append_decision_record(component: str, decision: str, rationale: str, status: Literal["proposed","accepted","superseded"])`
      — append to `decisions[]` in the graph. Append-only → recoverable → **ungated**.
- [ ] **T2.2** `verify_graph_integrity(scope: Literal["nodes","edges","all"])` — domain check
      over the graph: orphan edges, duplicate node ids, empty scan result where files exist.
      On failure returns `{"error": "graph_integrity_failed", "details": [...]}` — a
      structured error, **not** raised and **not** returned as if it were valid data.
      This is the tool error the loop branches on (Part B, B2).
- [ ] **T2.3** `prune_graph_node(node_id: str, cascade: Literal["node_only","node_and_edges"])`
      — permanently removes a node. **Irreversible** → add to `GATED` → proceeds only on
      explicit `y`. Docstring must say "use only after the user has explicitly confirmed."
- [ ] **T2.4** Two demo runs proving both gate branches: approved (prune happens) and
      declined (graph unchanged, model receives the `declined` result and reacts).
- [ ] **T2.5** `tests/smoke_hw1.py` — end-to-end: seeded graph → query returns expected node;
      corrupt fixture → error branch fires, loop does not treat it as data; max-step cap
      trips on a forced loop; gate declines block the write.
- [ ] **T2.6** *(stretch)* reversibility-first variant: soft-delete + `restore_graph_node`,
      and a note in `ARCHITECTURE.md` on whether the hard gate could downgrade to a notice
      (Part B, B5).
- [ ] **T2.7** Update `ARCHITECTURE.md`: component entries, gate rationale, decision records.

**Depends on:** T0.5, T0.6, T0.8, T1.5. **Blocks:** T3.2.

---

## Phase 3 — Integration (all three)

- [ ] **T3.1** Swap stubs for real implementations; one PR per tool module, owner-authored.
- [ ] **T3.2** Integration run: scan → query → append decision → attempt prune (declined,
      then approved) → integrity check. Record the trace.
- [ ] **T3.3** `README.md` filled in: setup, `.env`, how to run, one worked example, the
      tool table, ownership.
- [ ] **T3.4** `ARCHITECTURE.md` final pass — every component `Status: done`, decision log
      complete, gaps section updated.
- [ ] **T3.5** Demo script: which requirement each part of the run satisfies.

---

## Open questions / blockers

- [ ] Exact Zen model ids for `CHEAP` / `STRONG` + per-token prices (incl. cached rate) — **Slack**
- [ ] Do re-scans merge into the existing graph or replace it? — Alejandro to decide, record in §5
- [ ] Does `prune_graph_node` cascade to orphaned edges by default? — Dias to decide, record in §5
