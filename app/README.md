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

## Reset between runs

```bash
python app/reset_demo.py            # undo the demo's edit and its decision
python app/reset_demo.py --check    # report only; exit 1 if not reset
python app/reset_demo.py --reindex  # also rebuild the Postgres index
```

A demo run leaves exactly two marks: `app/theme.py` is edited by the `/change`
beat, and a decision lands on `Module:app.theme`. Reset undoes those two and
nothing else — the `DELETE` is scoped `WHERE symbol_uid = 'Module:app.theme'`,
never a bare `DELETE FROM decisions`, because that table is the authored layer
no process may regenerate.

**Reset does not cost you the index.** Graph nodes come from the file *tree*, and
summary cards describe what a module is *for* — "holds the page's colours" is
true whichever colour is in there — so restoring a constant invalidates neither,
and the Postgres chunks built from those cards are equally unaffected.
`app.theme` stays searchable throughout. `content_sha` is the one value that goes
stale during a run, and restoring `theme.py` to its committed bytes restores it
too. `--reindex` exists for the one case that does need it: you recorded a
decision *and* rebuilt the index mid-demo, which would leave a chunk for a
decision that no longer exists.

## The "what does the agent know?" panel

The second button on the page opens `app/` as the agent sees it: every `app.*`
graph node, and for each one its authored summary cards.

It shows the two layers side by side because their separation is the
architecture (#5, #57) — the **graph node** is derived and any scan may replace
it wholesale; the **summary card** is authored into the sqlite overlay and no
scan may touch it; they join on `symbol_uid` and are never merged. Under each
card is the chunk built from it, **fetched from Postgres by `symbol_uid`**, so
what you read is byte-for-byte the passage `search_corpus` returns when that
module is a hit — not a paraphrase reconstructed by the page.

Cards are written by `overlay/summarize.py` (author id `summarizer`), not by the
executor.

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

## Reading a row

Every row carries an **actor** (who) and a **kind** (what). The actor drives the
badge and the row's left rule; the kind tints the message.

| actor | who it is |
| --- | --- |
| `user` | a human |
| `qa-agent` | the channel's single read-only Q&A agent — **the only agent on the question path**; there is no planner or executor there |
| `planner` | `agents.planner` — seed, impact scope, constraints (change path only) |
| `executor` | `agents.executor` — the tool calls that implement the plan |
| `gate` | the approval gate; at that moment it is the human's turn |
| `system` | the process — boot, queue, transport |

Kinds: `boot` `user` `agent` `tool` `plan` `exec` `gate` `answer` `note` `error`.

A **question** shows `user → qa-agent → answer`. A **`/change`** shows
`user → planner → executor → gate`. If you only ever see `qa-agent`, you are
looking at the question path — that is correct, not missing attribution.

## The `[answers]` tag on search results

`search_corpus` rows read like:

```
5 passages, reranked — #1 app.theme [answers]; #2 guidance/04-slides.md [answers]
```

`answers` / `related` / `unrelated` are the reranker's three **named bands**
(`retrieval/rerank.py`), not similarity scores. `answers` means the reranker
judged the passage as directly answering the query. All five hits landing in the
top band is the reranker working, not a broken scorer.

The panel prints the band name rather than the raw value it is stored as
(`1.0`/`0.5`/`0.0`) because decision #65 chose named bands over a numeric score
precisely so a human could audit the judgement — rendering `1.0` reads as "100%
match", which is neither what it means nor something the reranker can know.

With `rerank: false` there are no bands, so rows show `rrf 0.0328` — the fusion
score, whose absolute value means nothing on its own, only its order.

## Resizing the panel

Drag the divider. Double-click it to reset. The width is remembered across
reloads, so you can set it once for the projector.

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
