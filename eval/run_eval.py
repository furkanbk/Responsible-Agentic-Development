"""eval.run_eval — the k-sweep and the rerank on/off matrix (HW5, T14.12).

Owner: Alejandro Ramírez Trueba (Phase 14C, Part 2).

Runs the five rank-aware metrics over the eval set, twice — reranking on and off,
through the real `rerank: bool` seam on `search()` (decision #60), so the table
measures the same code path the agent actually uses rather than a test-only branch
— and at three depths, k ∈ {3, 5, 10}. Precision@k and recall@k move in opposite
directions as k rises; that tension is the finding the sweep exists to show, not an
inconvenience to average away.

Every scored case is searched **once per rerank setting**, at `K_MAX`, and the k=3
and k=5 rows are truncations of that one ranking. That is exact for this reranker,
not a shortcut: it sorts the candidate pool into named bands and returns the top
`top_k` (decision #65), so the top-3 of a rerank-to-10 ranking is the same ordering
as a rerank-to-3 — and it halves the reranker's LLM calls.

The scoring core, `evaluate`, takes an injected `search_fn(query, k, rerank) ->
list[Hit]`, so the whole matrix runs offline in a test over a fake retriever with no
Postgres and no API key. `main()` wires the real `retrieval.search.search` over a
live index; it is the only part that needs the database up.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from retrieval.types import Hit

from .loader import ResolvedCase, load_cases, resolve_all
from .retrieval_metrics import METRIC_NAMES, aggregate, metrics_for_case, ranked_ids_from_hits

KS: tuple[int, ...] = (3, 5, 10)
K_MAX = max(KS)
RERANK_FLAGS: tuple[bool, ...] = (True, False)

# A search over a live index, or any stand-in with the same shape.
SearchFn = Callable[..., Sequence[Hit]]


def _score_case(
    resolved: ResolvedCase, search_fn: SearchFn, rerank: bool, ks: Sequence[int]
) -> dict[int, dict]:
    """One search at K_MAX for a scored case, metrics at every k off that ranking."""
    hits = search_fn(resolved.case.query, k=K_MAX, rerank=rerank)
    ranked = ranked_ids_from_hits(hits)          # retriever order, read from Hit.rank (#59)
    return {k: metrics_for_case(ranked, resolved.golden, k) for k in ks}


def evaluate(
    resolved_cases: Sequence[ResolvedCase],
    search_fn: SearchFn,
    *,
    ks: Sequence[int] = KS,
    rerank_flags: Sequence[bool] = RERANK_FLAGS,
) -> dict:
    """The full matrix. Pure given `search_fn`; see the module docstring.

    Only ``"scored"`` cases reach the retriever. Out-of-corpus cases (no anchors)
    and unresolved cases (anchors that resolved to nothing — a stale golden) are
    excluded from every average and counted separately, so a correct abstention
    and a broken eval row never masquerade as a retrieval score (assignment Part 2).
    """
    scored = [rc for rc in resolved_cases if rc.status == "scored"]
    out_of_corpus = [rc for rc in resolved_cases if rc.status == "out_of_corpus"]
    unresolved = [rc for rc in resolved_cases if rc.status == "unresolved"]

    runs: dict[bool, dict] = {}
    for rerank in rerank_flags:
        per_case: dict[str, dict[int, dict]] = {}
        for rc in scored:
            per_case[rc.case.case_id] = _score_case(rc, search_fn, rerank, ks)

        overall = {
            k: aggregate([per_case[cid][k] for cid in per_case]) for k in ks
        }

        by_category: dict[str, dict[int, dict]] = {}
        categories = sorted({rc.case.category for rc in scored})
        for cat in categories:
            ids = [rc.case.case_id for rc in scored if rc.case.category == cat]
            by_category[cat] = {
                k: aggregate([per_case[cid][k] for cid in ids]) for k in ks
            }

        runs[rerank] = {"overall": overall, "by_category": by_category, "per_case": per_case}

    return {
        "ks": list(ks),
        "rerank_flags": list(rerank_flags),
        "counts": {
            "scored": len(scored),
            "out_of_corpus": len(out_of_corpus),
            "unresolved": len(unresolved),
            "total": len(resolved_cases),
        },
        "out_of_corpus_cases": [rc.case.case_id for rc in out_of_corpus],
        "unresolved_cases": [rc.case.case_id for rc in unresolved],
        "runs": runs,
    }


# --- report formatting --------------------------------------------------------

def _fmt(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.3f}"


def _overall_table(results: dict, rerank: bool) -> str:
    ks = results["ks"]
    run = results["runs"][rerank]["overall"]
    header = "| metric | " + " | ".join(f"k={k}" for k in ks) + " |"
    sep = "|" + "|".join(["---"] * (len(ks) + 1)) + "|"
    lines = [header, sep]
    for name in METRIC_NAMES:
        cells = " | ".join(_fmt(run[k][name]) for k in ks)
        label = "MRR" if name == "mrr" else name.replace("_", " ")
        lines.append(f"| {label} | {cells} |")
    return "\n".join(lines)


def _category_table(results: dict, rerank: bool, k: int) -> str:
    by_cat = results["runs"][rerank]["by_category"]
    header = "| category | " + " | ".join(
        ("MRR" if n == "mrr" else n.replace("_", " ")) for n in METRIC_NAMES
    ) + " |"
    sep = "|" + "|".join(["---"] * (len(METRIC_NAMES) + 1)) + "|"
    lines = [header, sep]
    for cat in sorted(by_cat):
        cells = " | ".join(_fmt(by_cat[cat][k][name]) for name in METRIC_NAMES)
        lines.append(f"| {cat} | {cells} |")
    return "\n".join(lines)


def format_report(results: dict, *, category_k: int = 5) -> str:
    """Markdown ready to paste into README § Part 2."""
    c = results["counts"]
    out = [
        f"Cases: {c['total']} total — {c['scored']} scored, "
        f"{c['out_of_corpus']} out-of-corpus (undefined, excluded), "
        f"{c['unresolved']} unresolved (stale golden, excluded).",
    ]
    if results["unresolved_cases"]:
        out.append(f"Unresolved: {', '.join(results['unresolved_cases'])}")
    out.append("")
    out.append("**Reranking ON**")
    out.append(_overall_table(results, True))
    out.append("")
    out.append("**Reranking OFF**")
    out.append(_overall_table(results, False))
    out.append("")
    out.append(f"**Per category (k={category_k}, reranking ON)**")
    out.append(_category_table(results, True, category_k))
    out.append("")
    out.append(f"**Per category (k={category_k}, reranking OFF)**")
    out.append(_category_table(results, False, category_k))
    return "\n".join(out)


# --- CLI: the only part that needs a live index -------------------------------

def _live_search_fn(conn) -> SearchFn:
    """`search()` bound to one reused connection (cheaper than reconnecting per query)."""
    from retrieval.search import search

    def search_fn(query: str, *, k: int, rerank: bool) -> Sequence[Hit]:
        return search(query, k=k, rerank=rerank, conn=conn)

    return search_fn


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="HW5 Part 2 — retrieval metrics matrix")
    parser.add_argument("--cases", type=Path, default=None, help="path to cases.json")
    parser.add_argument("--json", type=Path, default=None, help="also write raw results here")
    parser.add_argument("--category-k", type=int, default=5, help="k for the per-category tables")
    args = parser.parse_args(argv)

    from retrieval.store import IndexUnavailable, all_chunks, connect

    cases = load_cases(args.cases)
    try:
        with connect() as conn:
            corpus = all_chunks(conn)
            if not corpus:
                print("index is empty — run `python -m retrieval.index` first", file=sys.stderr)
                return 2
            resolved = resolve_all(cases, corpus)
            results = evaluate(resolved, _live_search_fn(conn))
    except IndexUnavailable as exc:
        print(f"retrieval index unavailable: {exc}", file=sys.stderr)
        print("start it with `docker compose up -d` and index with "
              "`python -m retrieval.index`", file=sys.stderr)
        return 2

    print(format_report(results, category_k=args.category_k))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nraw results -> {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
