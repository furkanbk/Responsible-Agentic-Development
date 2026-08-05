"""tests.test_retrieval_metrics — the five rank metrics + the harness (HW5, T14.12).

Owner: Alejandro Ramírez Trueba (Phase 14C, Part 2).

**Offline by construction, and that is correct here** (T14.12): contract #10
(`Chunk` / `Hit` / `EvalCase`) is the entire input surface for the scorers, so a
handful of hand-built `Hit`s exercises every metric with no Postgres and no API
key. The scorers touch no model and no framework — there is nothing for a mocked
LLM to stand in for — so CLAUDE.md §8's "one real call per new suite" is satisfied
by the live retrieval suite (`test_retrieval_online.py`) and by the one online test
at the bottom of this file, which runs the whole harness (search → resolve → score)
against a real index. The offline body is where the metric *arithmetic* is pinned,
because a scorer nobody tested is a number nobody should trust.

Worked values are asserted against the definitions, not against a previous run, so
these tests would catch a scorer that drifted rather than merely a scorer that
changed.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _online import have_real_key  # noqa: E402

from eval import loader, run_eval  # noqa: E402
from eval.loader import ResolvedCase, resolve_anchor, resolve_case  # noqa: E402
from eval.retrieval_metrics import (  # noqa: E402
    aggregate,
    hit_rate_at_k,
    mean,
    metrics_for_case,
    ndcg_at_k,
    precision_at_k,
    ranked_ids_from_hits,
    recall_at_k,
    rr_at_k,
)
from retrieval.types import Anchor, Chunk, EvalCase, Hit  # noqa: E402


# --- helpers -----------------------------------------------------------------

def _chunk(chunk_id, *, kind="component", symbol_uid=None, symbol=None,
           heading_path="", text="") -> Chunk:
    return Chunk(chunk_id=chunk_id, kind=kind, text=text or chunk_id,
                 symbol_uid=symbol_uid, symbol=symbol, heading_path=heading_path)


def _hits(order):
    """`Hit`s whose LIST order is deliberately NOT their rank order.

    Reversed on purpose: a scorer that reads list position instead of `Hit.rank`
    would score these backwards, so this catches the decision #59 mistake.
    """
    n = len(order)
    return [Hit(chunk=_chunk(cid), rank=n - i) for i, cid in enumerate(order)]


# --- the metric arithmetic ----------------------------------------------------

def test_hit_rate_is_any_golden_in_top_k():
    ranked = ["a", "b", "c", "d"]
    assert hit_rate_at_k(ranked, {"c"}, 3) == 1.0
    assert hit_rate_at_k(ranked, {"d"}, 3) == 0.0      # d is at rank 4, outside top-3
    assert hit_rate_at_k(ranked, {"d"}, 4) == 1.0


def test_precision_denominator_is_what_was_returned():
    ranked = ["a", "b", "c", "d"]
    # two of the top-4 relevant -> 0.5
    assert precision_at_k(ranked, {"a", "c"}, 4) == 0.5
    # top-2 both relevant -> 1.0
    assert precision_at_k(ranked, {"a", "b"}, 2) == 1.0
    # fewer results than k: denominator is 2 (what was returned), not k=5
    assert precision_at_k(["a", "b"], {"a"}, 5) == 0.5


def test_recall_is_fraction_of_golden_found():
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, {"a", "c"}, 4) == 1.0
    assert recall_at_k(ranked, {"a", "z"}, 4) == 0.5   # z is not in the ranking at all
    assert recall_at_k(ranked, {"a", "b", "c"}, 2) == pytest.approx(2 / 3)


def test_mrr_is_reciprocal_of_the_first_hit_rank():
    ranked = ["a", "b", "c", "d"]
    assert rr_at_k(ranked, {"a"}, 4) == 1.0
    assert rr_at_k(ranked, {"c"}, 4) == pytest.approx(1 / 3)
    assert rr_at_k(ranked, {"c"}, 2) == 0.0            # first hit outside top-2


def test_ndcg_rewards_a_higher_rank():
    ranked = ["a", "b", "c", "d"]
    assert ndcg_at_k(ranked, {"a"}, 4) == pytest.approx(1.0)      # single golden at rank 1
    # single golden at rank 3: DCG = 1/log2(4), IDCG = 1/log2(2) = 1
    assert ndcg_at_k(ranked, {"c"}, 4) == pytest.approx(1 / math.log2(4))
    # the SAME golden higher up scores strictly better
    assert ndcg_at_k(ranked, {"b"}, 4) > ndcg_at_k(ranked, {"c"}, 4)


def test_empty_golden_is_undefined_not_zero():
    ranked = ["a", "b", "c"]
    for fn in (hit_rate_at_k, precision_at_k, recall_at_k, rr_at_k, ndcg_at_k):
        assert fn(ranked, set(), 3) is None, fn.__name__
    got = metrics_for_case(ranked, set(), 3)
    assert all(v is None for v in got.values())


def test_mean_skips_undefined_and_is_none_over_nothing():
    assert mean([1.0, None, 0.0]) == 0.5
    assert mean([None, None]) is None
    assert aggregate([{"hit_rate": None}, {"hit_rate": 1.0}])["hit_rate"] == 1.0


def test_ranked_ids_read_hit_rank_not_list_order():
    # _hits assigns rank = n-i, so list order [a,b,c,d] carries ranks [4,3,2,1]:
    # the true retriever order is the reverse, and the function must read Hit.rank.
    hits = _hits(["a", "b", "c", "d"])
    assert [h.chunk_id for h in hits] == ["a", "b", "c", "d"]      # list order
    assert ranked_ids_from_hits(hits) == ["d", "c", "b", "a"]      # rank order


# --- anchor resolution --------------------------------------------------------

def _corpus():
    return [
        _chunk("c_mod", kind="component", symbol_uid="Module:overlay.uid"),
        _chunk("c_sym", kind="component", symbol_uid="Module:overlay.uid", symbol="resolve_uid"),
        _chunk("c_other", kind="component", symbol_uid="Module:agentlib.loop"),
        _chunk("c_dec21", kind="doc",
               heading_path="docs/ARCHITECTURE.md > 5. Decision log > HW2 decisions",
               text="docs/ARCHITECTURE.md > 5. Decision log\n| # | Decision |\n"
                    "| 21 | The structural layer stays JSON |"),
        _chunk("c_dec22", kind="doc",
               heading_path="docs/ARCHITECTURE.md > 5. Decision log > HW2 decisions",
               text="docs/ARCHITECTURE.md > 5. Decision log\n| # | Decision |\n"
                    "| 22 | symbol_uid becomes real |"),
        _chunk("c_purpose", kind="doc",
               heading_path="docs/ARCHITECTURE.md > 1. Purpose", text="RADF is ..."),
    ]


def test_component_anchor_resolves_module_and_symbol_cards_separately():
    corpus = _corpus()
    # no symbol -> the module card only
    assert resolve_anchor(Anchor("component", "overlay.uid"), corpus) == {"c_mod"}
    # symbol given -> that symbol's card only
    assert resolve_anchor(Anchor("component", "overlay.uid", "resolve_uid"), corpus) == {"c_sym"}
    # a dotted path with no card resolves to nothing (not an error)
    assert resolve_anchor(Anchor("component", "does.not.exist"), corpus) == set()


def test_decision_anchor_matches_the_row_number_not_prose():
    corpus = _corpus()
    assert resolve_anchor(Anchor("decision", "21"), corpus) == {"c_dec21"}
    assert resolve_anchor(Anchor("decision", "#22"), corpus) == {"c_dec22"}   # leading # tolerated
    assert resolve_anchor(Anchor("decision", "99"), corpus) == set()


def test_doc_anchor_matches_on_heading_path():
    corpus = _corpus()
    assert resolve_anchor(Anchor("doc", "1. Purpose"), corpus) == {"c_purpose"}
    assert resolve_anchor(Anchor("doc", "Decision log"), corpus) == {"c_dec21", "c_dec22"}


def test_resolve_case_reports_unresolved_anchors():
    corpus = _corpus()
    case = EvalCase("x", "q", "why-question", anchors=(
        Anchor("decision", "21"), Anchor("decision", "999"),
    ))
    golden, unresolved = resolve_case(case, corpus)
    assert golden == {"c_dec21"}
    assert [a.ref for a in unresolved] == ["999"]


# --- the real eval set loads and is well-formed -------------------------------

def test_the_shipped_eval_set_meets_the_assignment_shape():
    cases = loader.load_cases()
    assert len(cases) >= 20
    categories = {c.category for c in cases}
    assert len(categories) >= 5, categories
    out_of_corpus = [c for c in cases if c.out_of_corpus]
    assert out_of_corpus, "at least one out-of-corpus (empty-golden) case is required"
    assert "out-of-corpus" in categories
    # every non-empty case declares at least one anchor with a usable ref
    for c in cases:
        if not c.out_of_corpus:
            assert c.anchors and all(a.ref for a in c.anchors), c.case_id


def test_case_ids_are_unique():
    cases = loader.load_cases()
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids))


# --- the harness matrix, over a fake retriever (no index needed) --------------

class _FakeRetriever:
    """Returns a fixed ranking per query, as `Hit`s in retriever order."""

    def __init__(self, rankings_on, rankings_off):
        self._on, self._off = rankings_on, rankings_off

    def __call__(self, query, *, k, rerank):
        order = (self._on if rerank else self._off).get(query, [])
        return [Hit(chunk=_chunk(cid), rank=i) for i, cid in enumerate(order[:k], start=1)]


def test_evaluate_excludes_out_of_corpus_and_unresolved_and_counts_them():
    corpus = _corpus()
    cases = [
        # scored: golden resolves, and the fake retriever ranks it first
        EvalCase("scored", "find21", "why-question", anchors=(Anchor("decision", "21"),)),
        # out-of-corpus: no anchors
        EvalCase("oob", "kubernetes", "out-of-corpus", anchors=()),
        # unresolved: anchor declared but resolves to nothing (stale golden)
        EvalCase("stale", "ghost", "why-question", anchors=(Anchor("decision", "999"),)),
    ]
    resolved = loader.resolve_all(cases, corpus)
    status = {rc.case.case_id: rc.status for rc in resolved}
    assert status == {"scored": "scored", "oob": "out_of_corpus", "stale": "unresolved"}

    fake = _FakeRetriever(
        rankings_on={"find21": ["c_dec21", "c_other"]},
        rankings_off={"find21": ["c_other", "c_dec21"]},
    )
    results = run_eval.evaluate(resolved, fake, ks=(3,))
    assert results["counts"] == {"scored": 1, "out_of_corpus": 1, "unresolved": 1, "total": 3}
    assert results["unresolved_cases"] == ["stale"]
    # rerank ON puts the golden at rank 1 -> perfect; OFF puts it at rank 2.
    assert results["runs"][True]["overall"][3]["mrr"] == pytest.approx(1.0)
    assert results["runs"][False]["overall"][3]["mrr"] == pytest.approx(0.5)


def test_report_formats_without_error():
    corpus = _corpus()
    cases = [EvalCase("scored", "find21", "why-question", anchors=(Anchor("decision", "21"),))]
    resolved = loader.resolve_all(cases, corpus)
    fake = _FakeRetriever({"find21": ["c_dec21"]}, {"find21": ["c_dec21"]})
    results = run_eval.evaluate(resolved, fake)
    report = run_eval.format_report(results)
    assert "Reranking ON" in report and "Reranking OFF" in report
    assert "why-question" in report


# --- online: the whole harness over a real index (CLAUDE.md §8) ---------------

pytestmark_online = pytest.mark.online


def _require_key(monkeypatch):
    from dotenv import dotenv_values
    root = Path(__file__).resolve().parent.parent
    key = (dotenv_values(root / ".env").get("OPENROUTER_API_KEY") or "").strip()
    if not key or key.endswith("...") or key == "sk-or-v1-...":
        pytest.skip("OPENROUTER_API_KEY unset or a placeholder — see .env.example")
    monkeypatch.setenv("OPENROUTER_API_KEY", key)


@pytest.mark.online
def test_online_harness_scores_a_real_retrieval(pg_dsn, monkeypatch):
    """search → resolve → score, end to end against a live index.

    The offline tests score a *fake* retriever, so nothing there proves anchor
    resolution lines up with the ids the real chunker mints, or that
    `ranked_ids_from_hits` reads a real `search()` result correctly. This does:
    index two component chunks, point one eval case's anchor at one of them, and
    assert the metric sees the hit. It is the integration bug class the mocked
    matrix cannot catch (CLAUDE.md §8).
    """
    _require_key(monkeypatch)

    from retrieval import store
    from retrieval.embed import embed_texts
    from retrieval.search import search

    # A component chunk whose symbol_uid a component anchor will resolve to.
    target = Chunk(
        chunk_id="c_target", kind="component",
        text="agentlib.guards.detect_stall — stops a run that repeats a tool call.",
        symbol_uid="Module:agentlib.guards", symbol="detect_stall",
        heading_path="agentlib.guards > detect_stall",
    )
    noise = Chunk(
        chunk_id="c_noise", kind="component",
        text="Boil the pasta in salted water until al dente.",
        symbol_uid="Module:kitchen.pasta", symbol="boil",
        heading_path="kitchen.pasta > boil",
    )
    with store.connect() as conn:
        store.ensure_schema(conn)
        store.replace_all(conn, [target, noise], embed_texts([target.text, noise.text]))
        corpus = store.all_chunks(conn)

        case = EvalCase(
            "live", "what stops a run that repeats itself", "lexical-vs-semantic",
            anchors=(Anchor("component", "agentlib.guards", "detect_stall"),),
        )
        resolved = loader.resolve_all([case], corpus)
        assert resolved[0].status == "scored"
        assert resolved[0].golden == {"c_target"}

        search_fn = lambda q, *, k, rerank: search(q, k=k, rerank=rerank, conn=conn)  # noqa: E731
        results = run_eval.evaluate(resolved, search_fn, ks=(3,), rerank_flags=(False,))

    assert results["counts"]["scored"] == 1
    assert results["runs"][False]["overall"][3]["hit_rate"] == 1.0
