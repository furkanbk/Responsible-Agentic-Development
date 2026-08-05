"""eval — the retrieval + generation evaluation harness (HW5).

Owner: Alejandro Ramírez Trueba (Phase 14C, Part 2 — retrieval metrics);
Dias Sarkytbaev (Phase 14D, Part 3 — generation metrics).

**The harness imports the agent, never the reverse** (ARCHITECTURE.md §3, the
`eval/` entry). `eval.run_eval` calls into `retrieval.search.search` and
`tools.retrieval_tools`; nothing under `retrieval/`, `agents/` or `tools/` imports
`eval/`. Keeping the dependency one-directional is what lets the eval set grow,
the metrics change, and cases be added without ever touching a line the agent
runs in production.

Two families of number, in the order they cost:

  * **rank-aware retrieval metrics** (this phase) — hit rate@k, precision@k,
    recall@k, MRR, nDCG@k. They need only a ranked list of chunk ids and a golden
    set, so they are free, reproduce exactly, and run first: a retrieval bug caught
    here is a judged-metric bill not paid downstream.
  * **judged generation metrics** (Phase 14D, Dias) — faithfulness, answer
    relevance, context precision/recall. An LLM call each, hand-rolled on
    `monitor/judge.py`'s pattern (decision #61).

`eval.retrieval_metrics` imports nothing but the standard library, and
`eval.loader` imports only `retrieval.types` (which drags in no `psycopg`), so the
scorers and the whole offline test suite run with no Postgres and no API key —
contract #10 (`Chunk` / `Hit` / `Anchor` / `EvalCase`) is the entire input surface.
"""
