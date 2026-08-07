"""tests.test_trace_adapters — trace -> scorer input, and the feedback pass (HW6, 15C).

Owner: Alejandro Ramírez Trueba.

The property that matters most is the one the whole phase is built on: **the scorers
do not change.** So the offline body proves the adapters produce exactly what the
unchanged scorers already take — a `Chunk` a generation helper accepts, a ranked-id
order the rank metrics read, a run record the judge grades — using hand-built span
stubs, because the adapter's contract is the span *shape*, not MLflow's implementation.

Two distinctions get their own tests because getting them wrong fails silently:

* **Retriever order, not list order.** A chunk dict that lost its rank on the way
  through a span must still rank where the retriever put it, or MRR and nDCG read the
  wrong position while the other three metrics look fine.
* **Missing instructions is `None`, not `""`.** The judge tells "ignored the rule"
  apart from "never had the rule"; an empty string quietly answers "never had it", so
  a run whose instructions could not be joined must be marked missing and skipped.

CLAUDE.md §8 applies — this suite touches the framework, so it carries an online test
that runs a real agent, adapts its trace, scores it with a real judged call, writes the
feedback back with MLflow, and reads it off the trace.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _online import online_key  # noqa: E402,F401  (pytest fixture)
from eval import generation_metrics as gm  # noqa: E402
from eval import run_trace_scoring as rts  # noqa: E402
from eval import trace_adapters as ta  # noqa: E402
from eval.retrieval_metrics import ranked_ids_from_hits  # noqa: E402
from tracing import spans as spans_mod  # noqa: E402

TOOL = spans_mod.TOOL_NAME_ATTR


# --- span/trace stubs (the shape the adapters read) ---------------------------

class _Span:
    def __init__(self, name, span_type, start, *, attributes=None, inputs=None,
                 outputs=None, parent_id=None, end=None):
        self.name = name
        self.span_type = span_type
        self.start_time_ns = start
        self.end_time_ns = end if end is not None else start + 1_000_000
        self.attributes = attributes or {}
        self.inputs = inputs
        self.outputs = outputs
        self.parent_id = parent_id


class _Info:
    def __init__(self, tags, trace_id):
        self.tags = tags
        self.trace_id = trace_id


class _Trace:
    def __init__(self, spans, *, tags=None, trace_id="tr-1"):
        self.data = type("D", (), {"spans": spans})()
        self.info = _Info(tags or {}, trace_id)


def _root(request="why json?", answer="because it diffs", stopped="answered"):
    return _Span("agent.run", "AGENT", 1, parent_id=None,
                 inputs={"request": request},
                 outputs={"answer": answer, "stopped": stopped})


def _retriever(hits, start=2):
    return _Span("retriever.search", "RETRIEVER", start,
                 parent_id="root", inputs={"query": "q"}, outputs={"hits": hits})


def _tool(name, args, start, *, branch="ok"):
    return _Span(f"tool.{name}", "TOOL", start, parent_id="root",
                 attributes={TOOL: name, "radf.branch": branch}, inputs=args)


# --- trace_to_rag_inputs ------------------------------------------------------

def test_rag_inputs_rebuild_chunks_in_retriever_order():
    """The chunks and ranked_ids come out in `Hit.rank` order, not list order."""
    hits = [{"chunk_id": "c3", "rank": 3, "text": "third", "kind": "doc"},
            {"chunk_id": "c1", "rank": 1, "text": "first", "kind": "component"},
            {"chunk_id": "c2", "rank": 2, "text": "second", "kind": "doc"}]
    trace = _Trace([_root(), _retriever(hits)])
    rag = ta.trace_to_rag_inputs(trace)

    assert rag["question"] == "why json?"
    assert rag["answer"] == "because it diffs"
    assert [c.chunk_id for c in rag["chunks"]] == ["c1", "c2", "c3"]
    assert rag["ranked_ids"] == ["c1", "c2", "c3"]
    # ranked_ids must agree with the frozen helper, never diverge from it.
    assert rag["ranked_ids"] == ranked_ids_from_hits(ta.hits_from_trace(trace))


def test_rag_inputs_map_chunk_fields_and_coerce_unknown_kind():
    """Every field a span recorded lands on the Chunk; an odd kind cannot crash."""
    hits = [{"chunk_id": "c1", "rank": 1, "text": "t", "kind": "mystery",
             "symbol_uid": "Fn:webhook.verify", "heading_path": "§Webhook",
             "source_path": "triggers/webhook.py"}]
    chunk = ta.trace_to_rag_inputs(_Trace([_root(), _retriever(hits)]))["chunks"][0]
    assert chunk.symbol_uid == "Fn:webhook.verify"
    assert chunk.heading_path == "§Webhook"
    assert chunk.kind == "doc"        # unknown kind coerced to the unscoped kind


def test_rag_inputs_fall_back_to_list_position_when_rank_is_missing():
    """A span that dropped `rank` still ranks in the order it recorded hits."""
    hits = [{"chunk_id": "a", "text": "x"}, {"chunk_id": "b", "text": "y"}]
    rag = ta.trace_to_rag_inputs(_Trace([_root(), _retriever(hits)]))
    assert rag["ranked_ids"] == ["a", "b"]


def test_rebuilt_chunks_feed_a_generation_helper_unchanged():
    """The Chunks the adapter builds are valid input to a scorer, no edits needed."""
    hits = [{"chunk_id": "c1", "rank": 1, "text": "alpha"},
            {"chunk_id": "c2", "rank": 2, "text": "beta"}]
    chunks = ta.trace_to_rag_inputs(_Trace([_root(), _retriever(hits)]))["chunks"]
    # context_text is the generation scorer's own pure helper; if the adapter's
    # Chunks were the wrong shape this would raise or drop text.
    ctx = gm.context_text(chunks)
    assert "alpha" in ctx and "beta" in ctx


# --- trace_to_run_record ------------------------------------------------------

def test_run_record_joins_instructions_and_reconstructs_steps():
    trace = _Trace(
        [_root(request="fix the bug"),
         _tool("search_corpus", {"query": "bug"}, 3),
         _tool("apply_change", {"path": "x.py"}, 4, branch="declined")],
        tags={"radf.run_id": "rid-7", "radf.agent": "agent-eval",
              "radf.user_id": "u1"})
    runs_index = {"rid-7": {"assembled": {"instructions": "RULE: gate writes",
                                          "data_blocks": [], "sources": []},
                            "thread_id": "th1"}}
    rec = ta.trace_to_run_record(trace, runs_index=runs_index)

    assert rec["run_id"] == "rid-7"
    assert rec["assembled"]["instructions"] == "RULE: gate writes"
    assert rec["instructions_source"] == "runs.jsonl"
    assert rec["request"] == "fix the bug"
    assert rec["stopped"] == "answered"
    assert [s["tool"] for s in rec["steps"]] == ["search_corpus", "apply_change"]
    assert rec["steps"][1]["branch"] == "declined"          # guard branch preserved
    assert "x.py" in rec["steps"][1]["args"]["path"]        # arguments preserved


def test_run_record_marks_missing_instructions_rather_than_blanking_them():
    """The one distinction the judge depends on: absent != empty."""
    trace = _Trace([_root()], tags={"radf.run_id": "unknown-rid"})
    rec = ta.trace_to_run_record(trace, runs_index={})
    assert rec["assembled"]["instructions"] is None
    assert rec["instructions_source"] == "missing"
    assert rec["assembled"]["instructions"] != ""


def test_load_runs_index_maps_by_run_id_and_skips_junk(tmp_path):
    log = tmp_path / "runs.jsonl"
    log.write_text(
        '{"run_id": "a", "answer": "one"}\n'
        "\n"                                   # blank line
        "not json at all\n"
        '{"run_id": "b", "answer": "two"}\n',
        encoding="utf-8")
    index = ta.load_runs_index(log)
    assert set(index) == {"a", "b"}
    assert index["b"]["answer"] == "two"


def test_load_runs_index_missing_file_is_empty_not_an_error(tmp_path):
    assert ta.load_runs_index(tmp_path / "nope.jsonl") == {}


# --- run_trace_scoring (the feedback computation, offline) ---------------------

def test_retrieval_feedbacks_cover_five_metrics_at_each_k():
    hits = [{"chunk_id": "g1", "rank": 1}, {"chunk_id": "x", "rank": 2}]
    trace = _Trace([_root(), _retriever(hits)], tags={"eval_case_id": "q01"})
    fbs = rts.feedbacks_for_trace(trace, golden={"g1"}, runs_index={},
                                  run_generation=False, run_monitor=False)
    names = {f.name for f in fbs}
    assert "retrieval.ndcg_at_5" in names
    assert "retrieval.hit_rate_at_3" in names
    assert len(fbs) == 5 * len(rts.KS)
    assert all(isinstance(f.value, float) for f in fbs)


def test_empty_golden_writes_no_retrieval_feedback_not_zeros():
    """Out-of-corpus is undefined (decision #67): no 0.0 that reads as a failure."""
    hits = [{"chunk_id": "x", "rank": 1}]
    trace = _Trace([_root(), _retriever(hits)])
    fbs = rts.feedbacks_for_trace(trace, golden=set(), runs_index={},
                                  run_generation=False, run_monitor=False)
    assert fbs == []


def test_monitor_is_skipped_when_instructions_could_not_be_joined():
    trace = _Trace([_root()], tags={"radf.run_id": "missing"})
    fbs = rts.feedbacks_for_trace(trace, golden=None, runs_index={},
                                  run_generation=False, run_monitor=True)
    assert fbs == []


def test_load_cases_by_id_merges_base_and_generation_extension():
    cases = rts.load_cases_by_id()
    assert "q01_impact_scope" in cases                       # a base case
    assert any(cid.startswith("g0") for cid in cases)        # a gen extension case


def test_a_collecting_sink_receives_each_feedback_once():
    """The write loop hands every computed feedback to the sink, keyed by trace id."""
    hits = [{"chunk_id": "g1", "rank": 1}]
    trace = _Trace([_root(), _retriever(hits)], tags={"eval_case_id": "q01"},
                   trace_id="tr-42")
    calls = []
    fbs = rts.feedbacks_for_trace(trace, golden={"g1"}, runs_index={},
                                  run_generation=False, run_monitor=False)
    for fb in fbs:
        calls.append((trace.info.trace_id, fb.name, fb.value, fb.rationale))
    assert len(calls) == len(fbs) == 5 * len(rts.KS)
    assert all(tid == "tr-42" for tid, _, _, _ in calls)


def test_dry_run_sink_prints_a_readable_line(capsys):
    rts._dry_run_sink("trace-abcdef012345", "retrieval.ndcg_at_5", 0.5, "")
    out = capsys.readouterr().out
    assert "retrieval.ndcg_at_5" in out and "0.5" in out


# --- online (CLAUDE.md §8) ----------------------------------------------------

@pytest.mark.online
def test_online_real_trace_adapted_scored_and_written_back(online_key, tmp_path, monkeypatch):
    """The full loop the offline body cannot stand in for: a real traced run, adapted,
    scored with a real judged call, feedback written to MLflow and read back off the
    trace. A mocked model never exercises the adapter against MLflow's real span shape.
    """
    monkeypatch.setenv("RADF_MLFLOW_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    from tracing.setup import init_tracing
    if not init_tracing("radf-test"):
        pytest.skip("mlflow not installed — online trace-scoring test skipped")

    import mlflow
    from agentlib.core import CHEAP
    from agentlib.loop import run_agent
    from agentlib.runlog import RunLog
    from agentlib.schemas import schema_for
    from tools.utility_tools import evaluate_expression
    from tracing import eval_case_scope, get_trace, request_origin_scope

    run_log = RunLog(agent="test", user_id="tester", request="")
    with request_origin_scope("batch"), eval_case_scope("t01"):
        result = run_agent(
            "What is 21 * 2? Use your tools.",
            [schema_for(evaluate_expression)],
            {"evaluate_expression": evaluate_expression},
            model=CHEAP, max_steps=4, verbose=False, run_log=run_log,
        )
    assert result["stopped"] == "answered"

    trace = get_trace(run_log.trace_id)
    assert trace is not None

    # Adapt, then score with a reference-free generation metric (no golden needed).
    rag = ta.trace_to_rag_inputs(trace)
    assert rag["question"] and rag["answer"]
    mr = gm.answer_relevance(rag["question"], rag["answer"])
    assert mr.score is not None, "a real judged call should produce a score"

    # Write it back onto the trace under the contract-#14 namespace, and read it off.
    rts._default_sink(run_log.trace_id, f"generation.{mr.name}", mr.score, mr.notes)
    refetched = get_trace(run_log.trace_id)
    names = [getattr(a, "name", None)
             for a in (getattr(refetched.info, "assessments", None) or [])]
    assert f"generation.{mr.name}" in names
