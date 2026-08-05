"""eval.run_gen_eval — the judged-metrics run (HW5, T14.14).

Owner: Dias Sarkytbaev (Phase 14D, Part 3).

Retrieve -> answer -> judge, over the 30-case generation set, with reranking on
and off. The shape deliberately mirrors `eval/run_eval.py`: a pure `evaluate_*`
core taking an injected `search_fn`, and a `main()` that is the only part needing
a live index. That is what lets the whole matrix run offline in a test with no
Postgres, no API key and no bill.

**Two asymmetries with Part 2, both deliberate.**

*The reranking-off run scores a subset.* Part 2's metrics are free, so it runs
everything twice. Every number here costs an LLM call, so the second run is the
stratified 14-case subset named in `gen_cases.json` — two cases from each of the
seven categories, so no category disappears from the comparison. The subset is
declared in the data file rather than chosen at run time, because a subset picked
after seeing the results is not a subset, it is a selection.

*Out-of-corpus cases run one metric, not four.* `abstention` — and it is reported
on its own line, never averaged in. Part 1 established that retrieval returns
confident junk for these at every stage; this is where we find out whether the
generator passes that junk on.

The report `format_report` prints is markdown, ready for README § Part 3, and it
carries its own honesty line: mean answer length (the verbosity-bias cap, checked
rather than asserted), how many judged verdicts the harness downgraded for an
unfindable quote, how many were ungradeable, and the measured dollar cost of the
run. "The rank metrics are free and these are not" is the assignment's ordering
argument, and it deserves a number rather than an adjective.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from agentlib.core import CHEAP, STRONG
from retrieval.types import Anchor, Chunk, EvalCase, Hit

from .answer import MAX_WORDS, Answer, answer_stats, generate_answer
from .generation_metrics import (
    ABSTENTION_BANDS,
    METRIC_NAMES,
    MetricResult,
    aggregate,
    score_case,
)
from .loader import load_cases
from .retrieval_metrics import mean

_HERE = Path(__file__).resolve().parent
DEFAULT_GEN_CASES = _HERE / "gen_cases.json"

#: Passages put in front of the generator. `search_corpus`'s own default, so the
#: harness measures the context the agent actually gets rather than a wider one
#: chosen to make the numbers look better.
DEFAULT_K = 5

SearchFn = Callable[..., Sequence[Hit]]


# --- the eval set -------------------------------------------------------------

def load_gen_cases(
    path: Optional[Path] = None, *, base: Optional[Path] = None
) -> tuple[list[EvalCase], list[str]]:
    """`(cases, rerank_off_subset_ids)` — the base set plus this phase's extras.

    `extends` in the file names the base set (Alejandro's `cases.json`), which is
    loaded through his loader so there is one parser for one format. Extending
    rather than duplicating is what keeps Parts 2 and 3 talking about the same
    questions; see the `_comment` block in `gen_cases.json`.
    """
    path = Path(path or DEFAULT_GEN_CASES)
    raw = json.loads(path.read_text(encoding="utf-8"))

    cases: list[EvalCase] = []
    extends = raw.get("extends")
    if extends:
        cases.extend(load_cases(base or (path.parent / extends)))

    for row in raw.get("cases", []):
        cases.append(
            EvalCase(
                case_id=row["case_id"],
                query=row["query"],
                category=row["category"],
                golden_answer=row.get("golden_answer", ""),
                anchors=tuple(
                    Anchor(kind=a["kind"], ref=str(a["ref"]), symbol=a.get("symbol"))
                    for a in row.get("anchors", [])
                ),
            )
        )

    seen = {case.case_id for case in cases}
    subset = [cid for cid in raw.get("rerank_off_subset", []) if cid in seen]
    return cases, subset


# --- one case -----------------------------------------------------------------

def _chunks_from(hits: Sequence[Hit]) -> list[Chunk]:
    """The retrieved chunks in RETRIEVER order (decision #59).

    Sorted on `Hit.rank` for the same reason `ranked_ids_from_hits` is: so a
    caller that reordered the list cannot silently change what the generator saw
    relative to what Part 2 scored.
    """
    return [hit.chunk for hit in sorted(hits, key=lambda h: (h.rank, h.chunk_id))]


def run_case(
    case: EvalCase,
    search_fn: SearchFn,
    *,
    k: int,
    rerank: bool,
    answer_model: str = CHEAP,
    judge_model: str = STRONG,
    use_cache: bool = True,
) -> dict:
    """Retrieve, answer, judge — one case, one rerank setting."""
    hits = search_fn(case.query, k=k, rerank=rerank)
    chunks = _chunks_from(hits)
    answer = generate_answer(case.query, chunks, model=answer_model, use_cache=use_cache)
    scores = score_case(
        case.query,
        answer.text,
        chunks,
        case.golden_answer,
        out_of_corpus=case.out_of_corpus,
        model=judge_model,
        use_cache=use_cache,
    )
    return {"case": case, "answer": answer, "scores": scores}


# --- the matrix ---------------------------------------------------------------

def _summarise(rows: Sequence[dict]) -> dict:
    """Aggregate one rerank setting's rows into the numbers the report prints."""
    scored = [row for row in rows if not row["case"].out_of_corpus]
    oob = [row for row in rows if row["case"].out_of_corpus]

    per_case = [row["scores"] for row in scored]
    by_category: dict[str, dict] = {}
    for category in sorted({row["case"].category for row in scored}):
        subset = [row["scores"] for row in scored if row["case"].category == category]
        by_category[category] = aggregate(subset)

    results: list[MetricResult] = [
        result for row in rows for result in row["scores"].values()
    ]
    answers: list[Answer] = [row["answer"] for row in rows]

    abstention_labels = [
        row["scores"]["abstention"].label
        for row in oob
        if row["scores"].get("abstention") and row["scores"]["abstention"].gradeable
    ]

    return {
        "overall": aggregate(per_case),
        "by_category": by_category,
        "counts": {
            "cases": len(rows),
            "scored": len(scored),
            "out_of_corpus": len(oob),
            "ungradeable": sum(1 for r in results if not r.gradeable),
            "undefined": sum(1 for r in results if r.gradeable and r.score is None),
            "quote_downgrades": sum(len(r.downgraded) for r in results),
            "dropped_verdicts": sum(len(r.dropped) for r in results),
        },
        "abstention": {
            "labels": {
                band: abstention_labels.count(band) for band in ABSTENTION_BANDS
            },
            "mean": mean(
                ABSTENTION_BANDS[label] for label in abstention_labels
            ) if abstention_labels else None,
            "cases": [row["case"].case_id for row in oob],
        },
        "answers": answer_stats(answers),
        "judged_cost": sum(r.cost for r in results),
        "per_case": {
            row["case"].case_id: {
                "category": row["case"].category,
                "answer": row["answer"].text,
                "context": list(row["answer"].context_ids),
                "scores": {
                    name: {
                        "score": result.score,
                        "label": result.label,
                        "gradeable": result.gradeable,
                        "downgraded": len(result.downgraded),
                        "notes": result.notes,
                    }
                    for name, result in row["scores"].items()
                },
            }
            for row in rows
        },
    }


def evaluate_generation(
    cases: Sequence[EvalCase],
    search_fn: SearchFn,
    *,
    k: int = DEFAULT_K,
    rerank_flags: Sequence[bool] = (True, False),
    subset_ids: Sequence[str] = (),
    answer_model: str = CHEAP,
    judge_model: str = STRONG,
    use_cache: bool = True,
) -> dict:
    """The judged matrix. Pure given `search_fn` — see the module docstring.

    `subset_ids` applies to the reranking-OFF run only; empty means "all cases",
    which is what an offline test wants and what a live run cannot afford.
    """
    runs: dict[bool, dict] = {}
    for rerank in rerank_flags:
        selected = list(cases)
        if not rerank and subset_ids:
            wanted = set(subset_ids)
            selected = [case for case in cases if case.case_id in wanted]
        rows = [
            run_case(
                case, search_fn, k=k, rerank=rerank,
                answer_model=answer_model, judge_model=judge_model, use_cache=use_cache,
            )
            for case in selected
        ]
        runs[rerank] = _summarise(rows)

    results = {
        "k": k,
        "rerank_flags": list(rerank_flags),
        "subset_ids": list(subset_ids),
        "answer_model": answer_model,
        "judge_model": judge_model,
        "runs": runs,
        "total_cost": sum(
            runs[flag]["judged_cost"] + runs[flag]["answers"]["cost"] for flag in runs
        ),
    }
    results["common"] = common_view(results)
    return results


def common_view(results: dict) -> Optional[dict]:
    """The rerank ON-vs-OFF comparison, restricted to cases BOTH runs scored.

    Without this the comparison is not one. The reranking-OFF run scores a subset
    (decision #69), so the two overall columns average *different case sets* — and
    since the per-category tables show the categories are far from equally easy, a
    difference between those columns is partly a difference in which questions were
    asked. The honest ON-vs-OFF number is over the intersection; the full-set column
    stays in the report as the headline quality figure, not as half of a comparison.
    """
    runs = results["runs"]
    if len(runs) < 2:
        return None

    per_run = {flag: runs[flag]["per_case"] for flag in runs}
    shared = sorted(set.intersection(*(set(cases) for cases in per_run.values())))
    scored = [
        cid for cid in shared
        if all(METRIC_NAMES[0] in per_run[flag][cid]["scores"] for flag in per_run)
    ]
    if not scored:
        return None

    def _agg(cids: Sequence[str], flag: bool) -> dict[str, Optional[float]]:
        return {
            name: mean(
                per_run[flag][cid]["scores"][name]["score"]
                for cid in cids
                if name in per_run[flag][cid]["scores"]
            )
            for name in METRIC_NAMES
        }

    categories = sorted({per_run[True][cid]["category"] for cid in scored})
    return {
        "case_ids": scored,
        "overall": {flag: _agg(scored, flag) for flag in per_run},
        "by_category": {
            category: {
                flag: _agg([cid for cid in scored
                            if per_run[True][cid]["category"] == category], flag)
                for flag in per_run
            }
            for category in categories
        },
    }


# --- report formatting --------------------------------------------------------

def _fmt(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.3f}"


def _label(name: str) -> str:
    return name.replace("_", " ")


def _overall_table(results: dict) -> str:
    flags = results["rerank_flags"]
    header = "| metric | " + " | ".join(
        ("rerank ON (all scored)" if flag else "rerank OFF (subset)") for flag in flags
    ) + " |"
    lines = [header, "|" + "|".join(["---"] * (len(flags) + 1)) + "|"]
    for name in METRIC_NAMES:
        cells = " | ".join(_fmt(results["runs"][flag]["overall"][name]) for flag in flags)
        lines.append(f"| {_label(name)} | {cells} |")
    return "\n".join(lines)


def _common_table(common: dict, *, category: Optional[str] = None) -> str:
    """ON vs OFF over the cases both runs scored — the like-for-like comparison."""
    block = common["by_category"][category] if category else common["overall"]
    lines = ["| metric | rerank ON | rerank OFF | delta |",
             "|---|---|---|---|"]
    for name in METRIC_NAMES:
        on, off = block[True][name], block[False][name]
        delta = "—" if (on is None or off is None) else f"{on - off:+.3f}"
        lines.append(f"| {_label(name)} | {_fmt(on)} | {_fmt(off)} | {delta} |")
    return "\n".join(lines)


def _category_table(results: dict, rerank: bool) -> str:
    by_cat = results["runs"][rerank]["by_category"]
    header = "| category | " + " | ".join(_label(n) for n in METRIC_NAMES) + " |"
    lines = [header, "|" + "|".join(["---"] * (len(METRIC_NAMES) + 1)) + "|"]
    for category in sorted(by_cat):
        cells = " | ".join(_fmt(by_cat[category][name]) for name in METRIC_NAMES)
        lines.append(f"| {category} | {cells} |")
    return "\n".join(lines)


def format_report(results: dict) -> str:
    """Markdown ready to paste into README § Part 3."""
    out: list[str] = []
    out.append(f"Judge: `{results['judge_model']}`  |  answers generated on "
               f"`{results['answer_model']}`  |  k={results['k']}")
    out.append("")
    out.append(_overall_table(results))
    out.append("")

    common = results.get("common")
    if common:
        out.append(f"**Like-for-like (the {len(common['case_ids'])} cases both runs scored)** — "
                   "the columns above average different case sets, so this is the ON/OFF "
                   "comparison and that one is the headline quality figure.")
        out.append(_common_table(common))
        out.append("")
        for category in sorted(common["by_category"]):
            out.append(f"*{category}*")
            out.append(_common_table(common, category=category))
            out.append("")

    for flag in results["rerank_flags"]:
        run = results["runs"][flag]
        counts = run["counts"]
        stats = run["answers"]
        name = "ON" if flag else "OFF"
        out.append(f"**Reranking {name}** — {counts['cases']} cases "
                   f"({counts['scored']} scored, {counts['out_of_corpus']} out-of-corpus).")
        if stats["mean_words"] is None:
            out.append("- answers: none")
        else:
            out.append(f"- answers: mean {stats['mean_words']:.0f} words, max "
                       f"{stats['max_words']} (cap {MAX_WORDS}), {stats['truncated']} truncated, "
                       f"{stats['cached']} replayed from cache")
        out.append(f"- judge: {counts['quote_downgrades']} claim(s) downgraded for an unfindable"
                   f" quote, {counts['dropped_verdicts']} verdict(s) dropped,"
                   f" {counts['ungradeable']} ungradeable, {counts['undefined']} undefined")
        abst = run["abstention"]
        if abst["cases"]:
            spread = ", ".join(f"{band}={count}" for band, count in abst["labels"].items())
            out.append(f"- out-of-corpus ({', '.join(abst['cases'])}): {spread}"
                       f"  -> abstention {_fmt(abst['mean'])}")
        out.append(f"- judged spend this run: ${run['judged_cost']:.4f}")
        out.append("")
        out.append(f"**Per category (reranking {name})**")
        out.append(_category_table(results, flag))
        out.append("")

    out.append(f"Total measured spend (answers + judge, cache misses only): "
               f"${results['total_cost']:.4f}")
    return "\n".join(out)


# --- CLI: the only part that needs a live index -------------------------------

def _live_search_fn(conn) -> SearchFn:
    """`search()` bound to one reused connection, as Part 2's harness does."""
    from retrieval.search import search

    def search_fn(query: str, *, k: int, rerank: bool) -> Sequence[Hit]:
        return search(query, k=k, rerank=rerank, conn=conn)

    return search_fn


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="HW5 Part 3 — judged generation metrics")
    parser.add_argument("--cases", type=Path, default=None, help="path to gen_cases.json")
    parser.add_argument("--k", type=int, default=DEFAULT_K, help="passages per answer")
    parser.add_argument("--limit", type=int, default=0,
                        help="score only the first N cases (a cheap smoke run)")
    parser.add_argument("--rerank-only", action="store_true",
                        help="skip the reranking-OFF run entirely")
    parser.add_argument("--no-cache", action="store_true",
                        help="re-ask the model instead of replaying stored replies")
    parser.add_argument("--json", type=Path, default=None, help="also write raw results here")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv()

    from retrieval.store import IndexUnavailable, connect

    cases, subset = load_gen_cases(args.cases)
    if args.limit:
        cases = cases[:args.limit]
        subset = [cid for cid in subset if cid in {c.case_id for c in cases}]

    flags: tuple[bool, ...] = (True,) if args.rerank_only else (True, False)

    try:
        with connect() as conn:
            results = evaluate_generation(
                cases,
                _live_search_fn(conn),
                k=args.k,
                rerank_flags=flags,
                subset_ids=subset,
                use_cache=not args.no_cache,
            )
    except IndexUnavailable as exc:
        print(f"retrieval index unavailable: {exc}", file=sys.stderr)
        print("start it with `docker compose up -d` and index with "
              "`python -m retrieval.index`", file=sys.stderr)
        return 2

    # Persist BEFORE rendering. A judged run costs real money, and the first live
    # one died in `print()` on a cp1251 console after every model call had already
    # been made and paid for — the verdicts survived only because they were cached.
    # Writing the results first means a display problem can cost a re-render, never
    # a re-run.
    if args.json:
        payload = {key: value for key, value in results.items() if key != "runs"}
        payload["runs"] = {str(flag): run for flag, run in results["runs"].items()}
        args.json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"raw results -> {args.json}", file=sys.stderr)

    report = format_report(results)
    try:
        print(report)
    except UnicodeEncodeError:
        # The report is markdown for a README, not console art; a console that
        # cannot render a character must not fail the run that produced it.
        sys.stdout.buffer.write(report.encode("utf-8", "replace") + b"\n")
    return 0


__all__ = [
    "DEFAULT_GEN_CASES",
    "DEFAULT_K",
    "load_gen_cases",
    "run_case",
    "evaluate_generation",
    "format_report",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
