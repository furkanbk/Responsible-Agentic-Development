# app/ — the demo surface

A deliberately silly product (**Billion Dollar Startup: funny memes** — one red
button, one random meme) sitting on top of a deliberately serious trace panel.
The joke is the point: the top two thirds are a product nobody would build an
agent for, and the bottom third is the agent that actually runs this repo,
narrating itself while somebody talks to it on Telegram.

**Nothing here is part of RADF.** `app/` imports no agent code, holds no session,
opens no overlay connection, and writes to no store. It is an observer over two
files the system already produces. Deleting `app/` changes nothing about how the
system behaves — which is the property that makes it safe to demo from.

## Run it

```bash
./app/run_demo.sh          # postgres, then service.py, then the web app
open http://127.0.0.1:8080

./app/run_demo.sh status   # what is running
./app/run_demo.sh stop     # stop the two background jobs (postgres stays up)
```

Drop images into `app/memes/` (`.png .jpg .jpeg .gif .webp .bmp .avif`). They are
gitignored — they are per-demo, and binary.

## Where the trace comes from

| source | what it gives | when |
| --- | --- | --- |
| `app/logs/service.log` | `service.py`'s own stdout — connected, message in, tools offered, planner/executor stage lines, gate prompts | **live**, as it happens |
| `store/runs/runs.jsonl` | the `RunLog` every finished turn flushes — every tool call with its args, its branch and its result; the planner's `impacted` set; the answer | when the turn **ends** |

Neither one alone is enough. stdout moves while the agent is thinking but is
coarse; the run record is exact but arrives at the end. The panel merges both,
which is why a turn shows a live "message received" line first and the tool-by-tool
detail a few seconds later.

Where they overlap, **stdout yields** — the run record says the same thing with
the tool calls, their branches and the planner's real `impacted` list, so
`[TOOLS] offered`, `[STOPPED]`, `[PLANNER] ok — …` and `[EXECUTOR]`'s closing
line are dropped rather than printed twice (see `DROP` in `server.py`). The
inbound message is deduped the same way: the live copy wins. What survives from
stdout is what is live-only (the message arriving, a gate waiting) or
stdout-only (the planner's retrieval, and its seed + hop cap).

The run record is replayed slightly out of file order on purpose: a `/change` run
shares one `RunLog` between both agents, so the executor's tool calls sit in
`steps` while the planner's envelope — the plan those calls implement — sits later
in `envelopes`. The panel emits the plan first. See `expand_run`.

## What the panel colours mean

`boot` infrastructure · `user` a human · `agent` who picked the turn up ·
`tool` a tool call · `plan` the planner's impact scope · `exec` the handover to
the executor · `gate` an approval prompt or a declined write · `answer` what went
back · `note` / `error` everything else.

A **declined** gate renders as `gate`, not `error`. A human saying no to an
irreversible write is the gate working, and colouring it red teaches a demo
audience the opposite of the lesson.

## Retrieval on the Telegram path

`search_corpus` is in `service.py`'s `_READ_TOOLS` (decision #70), so a question
asked over Telegram can call it and the panel shows the ranked passages, their
scores, and whether the reranker or the fused order produced them. It is a tool
the model *chooses* — a question that names a component outright will go straight
to `query_component_graph` instead, and that is correct (#60).

The `/change` path retrieves too, inside
`agents/planner.py::_retrieve_seed_candidates`. That one is a direct Python call,
not a model-chosen tool call (#66), so it reaches no `run_agent` trace — it is
picked up from the `[retrieval]` line the planner prints (#72) and rendered in
the same tool lane, because it is the same `search_corpus` with the same args and
the same results. Its detail is marked `[code-owned: planner seed retrieval]`, so
the row shows the step without implying the model chose to make it.

Ask a question phrased the way people phrase them ("which bit handles the
approval prompt", "why is the graph a JSON file") to see the retrieval arm work.
