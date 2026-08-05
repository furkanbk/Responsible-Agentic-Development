"""tests.test_eval_scorers — the judged generation metrics (HW5, T14.14).

Owner: Dias Sarkytbaev (Phase 14D, Part 3). New file.

**A scorer nobody tested is a number nobody should trust** (assignment Part 3),
and a judged scorer is worse than an unjudged one in that respect: it produces a
plausible float whatever happens, so a broken parse, a mis-mapped shuffle or a
quote check that never fires all look exactly like "the model scored it 0.6".

So the judge is SCRIPTED here — monkeypatching `<module>.call`, the same trick
`test_monitor.py` uses — and what is under test is the part of the verdict that
belongs to the CODE:

* the quote check that downgrades a `supported` claim the passages do not contain,
* the label vocabularies, and the refusal to score a verdict outside them,
* the rationale requirement that discards an unbacked band,
* `context_precision`'s de-shuffle: verdicts are collected in a shuffled order and
  must come back attached to the right chunk (a bias mitigation that silently
  scrambles its own results is worse than no mitigation),
* undefined-not-zero, everywhere it applies.

Two eval cases from the real `gen_cases.json` are used as fixtures rather than
invented strings, so a case file that drifts out of shape fails here (assignment
Part 3: "using a couple of eval cases as a fixture").

The online test at the bottom makes one real judged call (CLAUDE.md §8): the
scripted tests prove the arithmetic, and only a live call proves that a real
model's reply survives `_parse_object` and the quote check at all.

Run:  python -m pytest tests/test_eval_scorers.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _online import online_key  # noqa: E402,F401 — pytest fixture, used by name

from agentlib.core import Result  # noqa: E402
from eval import answer as answer_mod  # noqa: E402
from eval import cache as cache_mod  # noqa: E402
from eval import generation_metrics as gm  # noqa: E402
from eval import run_gen_eval as harness  # noqa: E402
from retrieval.types import Chunk, Hit  # noqa: E402


# --- fixtures ----------------------------------------------------------------

GRAPH_TEXT = (
    "agentlib.graph > storage\n"
    "The structural graph is stored as a single JSON file because it is diffable "
    "in pull requests and any scan may replace it wholesale."
)
GATE_TEXT = (
    "tools.graph_write > prune_graph_node\n"
    "prune_graph_node is the only gated tool: it proceeds solely on explicit human "
    "confirmation, and its cascade parameter is required with no default."
)


def _chunk(chunk_id: str, text: str, *, heading_path: str = "doc > section") -> Chunk:
    return Chunk(chunk_id=chunk_id, kind="doc", text=text, heading_path=heading_path)


def _hits(*chunks: Chunk) -> list[Hit]:
    return [Hit(chunk=c, rank=i, rrf_score=1.0 / i) for i, c in enumerate(chunks, start=1)]


CHUNKS = [_chunk("c1", GRAPH_TEXT), _chunk("c2", GATE_TEXT)]


def script(monkeypatch, module, payload) -> None:
    """Make `module.call` return exactly `payload` (dict -> JSON, or raw text)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(module, "call", lambda *a, **k: Result(text=text))


def gen_cases() -> dict[str, object]:
    """The real generation eval set, keyed by case id — the fixture for these tests."""
    cases, _ = harness.load_gen_cases()
    return {case.case_id: case for case in cases}


# --- the eval set itself ------------------------------------------------------

def test_gen_case_file_meets_the_assignment_shape():
    """>=20 cases, >=5 failure categories, >=1 out-of-corpus, no duplicate ids."""
    cases, subset = harness.load_gen_cases()
    ids = [case.case_id for case in cases]

    assert len(cases) >= 20, "assignment Part 3 requires at least 20 cases"
    assert len(ids) == len(set(ids)), "duplicate case ids"
    assert len({case.category for case in cases}) >= 5
    assert any(case.out_of_corpus for case in cases), "one case must be out-of-corpus"
    # Every answerable case needs a reference answer: context_recall has nothing
    # to measure without one, and it would be scored undefined and vanish quietly.
    assert all(case.golden_answer.strip() for case in cases if not case.out_of_corpus)


def test_gen_cases_extend_the_part_2_set_rather_than_replacing_it():
    """The base set is loaded in, so Parts 2 and 3 describe the same queries."""
    from eval.loader import load_cases

    base_ids = {case.case_id for case in load_cases()}
    all_ids = {case.case_id for case in harness.load_gen_cases()[0]}
    assert base_ids < all_ids, "gen_cases.json must extend cases.json, not replace it"


def test_rerank_off_subset_is_stratified_and_resolvable():
    """Every named subset id exists, and every category survives into the subset."""
    cases, subset = harness.load_gen_cases()
    by_id = {case.case_id: case for case in cases}

    assert subset, "the reranking-off run needs a declared subset"
    assert all(cid in by_id for cid in subset)
    subset_categories = {by_id[cid].category for cid in subset}
    assert subset_categories == {case.category for case in cases}, (
        "a random subset can drop a whole failure category — this one must not"
    )


# --- the quote check (the deterministic core) --------------------------------

def test_quote_is_present_accepts_exact_and_elided_quotes():
    assert gm.quote_is_present("stored as a single JSON file because it is diffable", GRAPH_TEXT)
    # Whitespace and case are normalised; a judge that re-wrapped a line is not lying.
    assert gm.quote_is_present("STORED AS A SINGLE   JSON FILE because it is diffable", GRAPH_TEXT)


def test_quote_is_present_rejects_invented_and_trivial_quotes():
    assert not gm.quote_is_present(
        "the structural graph is stored in a sharded document database", GRAPH_TEXT
    )
    # Too short to be evidence of anything — "the graph" appears everywhere.
    assert not gm.quote_is_present("the graph", GRAPH_TEXT)


def test_faithfulness_downgrades_a_supported_claim_with_an_unfindable_quote(monkeypatch):
    """The judge asserts grounding it cannot show; the harness overrules it.

    This is the guarantee that separates this scorer from "ask a model for a
    number": one claim is genuinely quoted, one is supported by an invented
    quote, and the score must be 0.5 rather than the 1.0 the judge proposed.
    """
    script(monkeypatch, gm, {"claims": [
        {"claim": "the graph is a JSON file",
         "label": "supported",
         "quote": "The structural graph is stored as a single JSON file"},
        {"claim": "the graph is replicated across three regions",
         "label": "supported",
         "quote": "the graph is replicated across three availability regions"},
    ]})

    result = gm.faithfulness("...", CHUNKS, use_cache=False)

    assert result.score == pytest.approx(0.5)
    assert len(result.downgraded) == 1
    assert result.items[1]["label"] == "unsupported"
    assert result.items[1]["downgraded_from"] == "supported"


def test_faithfulness_downgrade_never_lands_on_contradicted(monkeypatch):
    """`contradicted` and `unsupported` are both worth 0.0 and mean opposite things.

    "The passages do not say this" must never be recorded as "the passages say the
    opposite" — the score is identical, the audit trail is not.
    """
    script(monkeypatch, gm, {"claims": [
        {"claim": "invented", "label": "supported", "quote": "nothing like this appears at all"},
    ]})
    result = gm.faithfulness("...", CHUNKS, use_cache=False)
    assert result.items[0]["label"] == "unsupported"


def test_faithfulness_is_undefined_for_a_refusal(monkeypatch):
    """A refusal makes no factual claims, so it is not unfaithful — it is undefined."""
    script(monkeypatch, gm, {"claims": []})
    result = gm.faithfulness("The passages do not cover this.", CHUNKS, use_cache=False)
    assert result.score is None
    assert result.gradeable is True          # undefined and ungradeable are different


def test_faithfulness_is_ungradeable_when_the_judge_returns_junk(monkeypatch):
    script(monkeypatch, gm, "I could not complete this task.")
    result = gm.faithfulness("...", CHUNKS, use_cache=False)
    assert result.score is None and result.gradeable is False


def test_faithfulness_drops_a_claim_whose_label_is_not_in_the_vocabulary(monkeypatch):
    script(monkeypatch, gm, {"claims": [
        {"claim": "the graph is a JSON file", "label": "mostly_true", "quote": ""},
        {"claim": "prune is gated", "label": "unsupported", "quote": ""},
    ]})
    result = gm.faithfulness("...", CHUNKS, use_cache=False)
    assert len(result.dropped) == 1
    assert result.score == pytest.approx(0.0)   # only the well-formed claim counts


# --- answer relevance ---------------------------------------------------------

@pytest.mark.parametrize("band,expected", [
    ("answers", 1.0), ("partially_answers", 0.5), ("evades", 0.0),
])
def test_answer_relevance_maps_named_bands_to_scores(monkeypatch, band, expected):
    script(monkeypatch, gm, {"label": band, "rationale": "asked X, answered Y"})
    result = gm.answer_relevance("why JSON?", "because it is diffable", use_cache=False)
    assert result.score == pytest.approx(expected)
    assert result.label == band


def test_answer_relevance_discards_a_verdict_with_no_rationale(monkeypatch):
    """Same rule as `monitor/judge.py`: an unbacked verdict is not a measurement."""
    script(monkeypatch, gm, {"label": "answers", "rationale": "   "})
    result = gm.answer_relevance("why JSON?", "because it is diffable", use_cache=False)
    assert result.score is None and result.gradeable is False
    assert result.dropped


def test_answer_relevance_rejects_a_score_shaped_reply(monkeypatch):
    """A judge that answers with a number instead of a band is not graded on it."""
    script(monkeypatch, gm, {"label": "8", "rationale": "pretty good"})
    result = gm.answer_relevance("why JSON?", "because it is diffable", use_cache=False)
    assert result.score is None and result.gradeable is False


# --- context precision, and the de-shuffle -----------------------------------

def test_shuffled_order_is_a_stable_permutation_and_not_the_identity():
    order = gm._shuffled_order("why is the graph a JSON file", 6)
    assert sorted(order) == list(range(6))
    assert order == gm._shuffled_order("why is the graph a JSON file", 6)   # replays
    # Deterministic per question, so at least one of several must be reordered —
    # otherwise the position-bias mitigation is not doing anything.
    questions = ["a", "why JSON", "what is gated", "who owns this", "when does it stall"]
    assert any(gm._shuffled_order(q, 6) != list(range(6)) for q in questions)


def test_context_precision_maps_verdicts_back_through_the_shuffle(monkeypatch):
    """The judge sees the passages shuffled; the result must name the right chunks.

    A mitigation that scrambles its own output would report precision on the
    wrong passages while looking perfectly healthy, so the marked chunk is found
    in the prompt at whatever position it was shown at, and the verdict must come
    back attached to it.
    """
    marked = _chunk("gold", "GOLDEN-MARKER: this passage answers the question")
    chunks = [_chunk(f"n{i}", f"filler passage {i}") for i in range(1, 5)]
    chunks.insert(2, marked)                      # retriever rank 3

    def fake_call(prompt=None, **kwargs):
        # Find which shown position carries the marker, and call only that relevant.
        shown_at = None
        for block in prompt.split("\n\n"):
            if "GOLDEN-MARKER" in block:
                shown_at = int(block.split("]")[0].lstrip("["))
        lines = [
            f"{i}: {'relevant' if i == shown_at else 'irrelevant'}"
            for i in range(1, len(chunks) + 1)
        ]
        return Result(text="\n".join(lines))

    monkeypatch.setattr(gm, "call", fake_call)
    result = gm.context_precision("which passage answers this", chunks, use_cache=False)

    assert result.score == pytest.approx(1 / 5)
    relevant = [item for item in result.items if item["label"] == "relevant"]
    assert [item["chunk_id"] for item in relevant] == ["gold"]
    assert relevant[0]["rank"] == 3                       # retriever order, not shown order
    assert [item["rank"] for item in result.items] == [1, 2, 3, 4, 5]


def test_context_precision_is_undefined_with_no_passages():
    result = gm.context_precision("anything", [], use_cache=False)
    assert result.score is None


def test_context_precision_excludes_an_unjudged_passage(monkeypatch):
    """Silence about a passage is not a verdict on it."""
    script(monkeypatch, gm, "1: relevant")
    result = gm.context_precision("q", CHUNKS, use_cache=False)
    assert result.score == pytest.approx(1.0)   # one judged, one dropped — not 0.5
    assert len(result.dropped) == 1


# --- context recall -----------------------------------------------------------

def test_context_recall_is_undefined_without_a_reference_answer():
    """The out-of-corpus case: nothing to recall, so not a recall of zero."""
    result = gm.context_recall("", CHUNKS, use_cache=False)
    assert result.score is None


def test_context_recall_counts_covered_reference_claims(monkeypatch):
    script(monkeypatch, gm, {"claims": [
        {"claim": "the graph is a JSON file", "label": "present",
         "quote": "The structural graph is stored as a single JSON file"},
        {"claim": "it is rebuilt nightly by a cron job", "label": "absent", "quote": ""},
    ]})
    result = gm.context_recall("The graph is a JSON file, rebuilt nightly.", CHUNKS,
                               use_cache=False)
    assert result.score == pytest.approx(0.5)


def test_context_recall_scores_zero_when_nothing_was_retrieved():
    """Distinct from undefined: there IS a reference answer and no context covers it."""
    result = gm.context_recall("The graph is a JSON file.", [], use_cache=False)
    assert result.score == pytest.approx(0.0)


# --- abstention (out-of-corpus only) -----------------------------------------

@pytest.mark.parametrize("band,expected", [
    ("abstains", 1.0), ("hedges", 0.5), ("answers_anyway", 0.0),
])
def test_abstention_bands(monkeypatch, band, expected):
    script(monkeypatch, gm, {"label": band, "rationale": "..."})
    result = gm.abstention("who is on call", "the passages do not say", use_cache=False)
    assert result.score == pytest.approx(expected)


def test_out_of_corpus_case_runs_only_the_abstention_metric(monkeypatch):
    """Four metrics over a question the corpus cannot answer would say nothing —
    and would cost four judged calls to say it."""
    script(monkeypatch, gm, {"label": "abstains", "rationale": "refused"})
    scores = gm.score_case("who is on call", "The passages do not cover this.",
                           CHUNKS, "", out_of_corpus=True, use_cache=False)
    assert set(scores) == {"abstention"}


# --- aggregation --------------------------------------------------------------

def test_aggregate_skips_undefined_rather_than_scoring_it_zero():
    """Decision #67, applied to the judged half: `None` must not drag a mean down."""
    per_case = [
        {name: gm.MetricResult(name=name, score=1.0) for name in gm.METRIC_NAMES},
        {name: gm.MetricResult(name=name, score=None) for name in gm.METRIC_NAMES},
    ]
    assert gm.aggregate(per_case)["faithfulness"] == pytest.approx(1.0)


def test_aggregate_is_none_when_every_case_is_undefined():
    per_case = [{name: gm.MetricResult(name=name, score=None) for name in gm.METRIC_NAMES}]
    assert gm.aggregate(per_case)["faithfulness"] is None


# --- the cache ----------------------------------------------------------------

def test_cache_replays_a_verdict_instead_of_asking_again(monkeypatch):
    """Reproducibility, not thrift: an un-cached judged run gives a different table
    every time, and there is no way to tell that from a real change."""
    calls = {"n": 0}

    def counting_call(*args, **kwargs):
        calls["n"] += 1
        return Result(text=json.dumps({"label": "answers", "rationale": "ok"}),
                      usage={"input_tokens": 10, "output_tokens": 5})

    monkeypatch.setattr(gm, "call", counting_call)
    first = gm.answer_relevance("q", "a", use_cache=True)
    second = gm.answer_relevance("q", "a", use_cache=True)

    assert calls["n"] == 1
    assert first.score == second.score
    assert second.cached is True
    assert second.cost == 0.0          # a replay is free, and must not be billed twice


def test_cache_key_changes_with_the_prompt_version():
    a = cache_mod.call_key("faithfulness", "m", "gen-v1", ["x"])
    b = cache_mod.call_key("faithfulness", "m", "gen-v2", ["x"])
    assert a != b, "an edited prompt must not replay verdicts from the old one"


# --- the whole matrix, offline ------------------------------------------------

def _scripted_judge(prompt=None, system=None, **kwargs):
    """One fake model standing in for all five judged calls, dispatched on the prompt."""
    if "grounded in the PASSAGES" in (system or ""):
        return Result(text=json.dumps({"claims": [
            {"claim": "the graph is a JSON file", "label": "supported",
             "quote": "The structural graph is stored as a single JSON file"},
        ]}))
    if "addresses the QUESTION" in (system or ""):
        return Result(text=json.dumps({"label": "answers", "rationale": "it answers"}))
    if "which retrieved PASSAGES are useful" in (system or ""):
        return Result(text="1: relevant\n2: irrelevant")
    if "contain what a REFERENCE" in (system or ""):
        return Result(text=json.dumps({"claims": [
            {"claim": "x", "label": "present",
             "quote": "The structural graph is stored as a single JSON file"},
        ]}))
    return Result(text=json.dumps({"label": "abstains", "rationale": "refused"}))


def test_the_matrix_runs_offline_and_applies_the_subset_only_to_rerank_off(monkeypatch):
    """The harness end to end with no Postgres, no key and no bill.

    Also pins the asymmetry that costs money in the live run: reranking ON scores
    every case, reranking OFF scores only the declared subset.
    """
    monkeypatch.setattr(gm, "call", _scripted_judge)
    monkeypatch.setattr(answer_mod, "call",
                        lambda *a, **k: Result(text="The graph is a JSON file."))

    cases, _ = harness.load_gen_cases()
    cases = cases[:6] + [case for case in cases if case.out_of_corpus][:1]
    subset = [cases[0].case_id, cases[1].case_id]

    results = harness.evaluate_generation(
        cases,
        lambda query, *, k, rerank: _hits(*CHUNKS[:k]),
        subset_ids=subset,
        use_cache=False,
    )

    assert results["runs"][True]["counts"]["cases"] == len(cases)
    assert results["runs"][False]["counts"]["cases"] == len(subset)
    assert results["runs"][True]["overall"]["faithfulness"] == pytest.approx(1.0)
    assert set(results["runs"][True]["overall"]) == set(gm.METRIC_NAMES)
    # The report must render without a live index — it is what goes in the README.
    assert "faithfulness" in harness.format_report(results)


def test_the_harness_feeds_the_generator_retriever_order(monkeypatch):
    """Decision #59 on the generation side: Part 2 scored this order, so Part 3
    must build its answer on the same one, or a disagreement between the two
    tables could be blamed on a repack instead of on the generator."""
    seen: dict[str, tuple] = {}

    def capture(question, chunks, **kwargs):
        seen["ids"] = tuple(c.chunk_id for c in chunks)
        return answer_mod.Answer(text="x", model="m",
                                 context_ids=tuple(c.chunk_id for c in chunks))

    monkeypatch.setattr(harness, "generate_answer", capture)
    monkeypatch.setattr(gm, "call", _scripted_judge)

    shuffled = [Hit(chunk=CHUNKS[1], rank=2), Hit(chunk=CHUNKS[0], rank=1)]
    case = gen_cases()["q08_why_json_graph"]
    harness.run_case(case, lambda *a, **k: shuffled, k=2, rerank=True, use_cache=False)

    assert seen["ids"] == ("c1", "c2")      # sorted by Hit.rank, not by list order


# --- online (CLAUDE.md §8) ----------------------------------------------------

@pytest.mark.online
def test_online_judge_separates_a_grounded_answer_from_an_invented_one(online_key):
    """One real judged call — the thing no scripted test can vouch for.

    The offline suite proves the arithmetic and the quote check. It cannot prove
    that a real judge's reply parses at all, that it uses the label vocabulary it
    was given, or that the quote check tolerates a real model's quoting habits —
    which is exactly the bug class §8 exists for. Two answers over the same
    passage, one quoting it and one inventing a data centre, must not score the
    same.
    """
    grounded = ("The structural graph is stored as a single JSON file because it is "
                "diffable in pull requests.")
    invented = ("The structural graph is stored in a sharded document database "
                "replicated across three regions with nightly snapshots.")

    good = gm.faithfulness(grounded, CHUNKS, use_cache=False)
    bad = gm.faithfulness(invented, CHUNKS, use_cache=False)

    assert good.gradeable and bad.gradeable
    assert good.score is not None and bad.score is not None
    assert good.score > bad.score, (
        f"judge did not separate grounded from invented: {good.score} vs {bad.score}"
    )
