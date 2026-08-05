"""eval.generation_metrics — the judged metrics (HW5, T14.14).

Owner: Dias Sarkytbaev (Phase 14D, Part 3).

Part 2's five metrics are free, exact and reproduce byte-for-byte. These four are
none of those things: each one costs an LLM call, and the thing producing the
number is itself a language model. So the whole design question is *how much of
the verdict is allowed to be the model's*.

**The answer is the same as `monitor/judge.py`'s: the model proposes, the code
decides** (decision #37, #61). Concretely, in every scorer below:

* The model is never asked for a **number**. It is asked to sort things into
  **named** buckets — `supported` / `contradicted` / `unsupported`, `relevant` /
  `irrelevant`, `answers` / `partially_answers` / `evades`. A model asked to rate
  faithfulness 0-10 returns 7 for almost everything; asked whether *this claim* is
  stated in *these passages* it has to commit to something a human can check.
* The **score is arithmetic over those labels**, computed here. No scorer reads a
  score out of the model's reply, because a self-reported score is exactly the
  number nobody can audit.
* A verdict the code can check, the code **checks**. `faithfulness` and
  `context_recall` require a verbatim quote for every claim they call supported,
  and a quote that is not actually in the passages is **downgraded here** — the
  judge does not get to assert grounding it cannot show. The count of downgrades
  is reported, so "the judge hallucinated support N times" is a measured number
  in this harness rather than a worry in the write-up.
* A truncated or unparseable reply is **ungradeable, never guessed** — the same
  branch `judge_run` takes, for the same reason: an unverifiable verdict is
  indistinguishable from an invented one.

**Undefined is not zero** (decision #67, and Part 2's convention). An answer with
no factual claims — a correct refusal on an out-of-corpus question — has no
faithfulness, not a faithfulness of 0. Every scorer returns `None` there, `mean`
skips it, and the harness counts it separately. Scoring a correct abstention as 0
would make the safest possible behaviour look like the worst.

**Judge bias, and what is actually done about it here** (assignment Part 3; the
README carries the same list in prose):

* *Self-preference* — the generator is `CHEAP` and the judge is `STRONG`. They are
  different models, so the judge is not grading its own output; they are from one
  family behind one provider, so this is **reduced, not eliminated**, and the repo
  has no second vendor to cross-check against. Said plainly rather than claimed away.
* *Position* — `context_precision` shows the passages in a **deterministically
  shuffled** order (seeded from the question, so it replays) and maps the verdicts
  back afterwards. Retriever order goes into the judge unshuffled only where it is
  the thing being measured, which for a per-passage relevance call it is not.
* *Verbosity* — answers are capped at ~120 words (`eval/answer.py`), and the two
  claim-based metrics are **ratios over claims**, so padding an answer with filler
  dilutes its faithfulness rather than inflating it. Two independent defences,
  because a word cap in a prompt is a request, not a guarantee.
* *Judge-model mismatch* — the judge is only ever shown material the answer was
  actually built from, never the retriever's internals or the golden ids, so it
  cannot reward an answer for agreeing with a ranking it never saw.

Costs are accumulated and reported per run. That is not bookkeeping for its own
sake: "the rank metrics are free and these are not" is the assignment's ordering
argument, and the README states it with a measured dollar figure.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from agentlib.core import STRONG, call, estimate_cost
from retrieval.types import Chunk

from .cache import call_key, get_call, put_call
from .retrieval_metrics import mean

#: Bump on ANY prompt edit below — the cache key carries it (`eval.cache.call_key`).
PROMPT_VERSION = "gen-v1"

#: The four judged metrics, in the order the report tables present them.
METRIC_NAMES: tuple[str, ...] = (
    "faithfulness",
    "answer_relevance",
    "context_precision",
    "context_recall",
)

# --- the named label vocabularies --------------------------------------------
#
# Every one of these is a set of names, never a scale. The float beside each name
# is applied HERE, after the model has committed to a word.

CLAIM_LABELS: dict[str, float] = {
    "supported": 1.0,        # the passages state it
    "contradicted": 0.0,     # the passages state the opposite
    "unsupported": 0.0,      # the passages neither state nor contradict it
}

COVERAGE_LABELS: dict[str, float] = {
    "present": 1.0,          # the passages contain this reference claim
    "absent": 0.0,
}

RELEVANCE_BANDS: dict[str, float] = {
    "answers": 1.0,
    "partially_answers": 0.5,
    "evades": 0.0,
}

PASSAGE_LABELS: dict[str, float] = {
    "relevant": 1.0,
    "irrelevant": 0.0,
}

#: Out-of-corpus cases only. Scored separately from the four, never averaged into
#: them: the correct behaviour here is refusal, and a metric family built to
#: reward answering would punish it.
ABSTENTION_BANDS: dict[str, float] = {
    "abstains": 1.0,          # says outright that the passages do not cover it
    "hedges": 0.5,            # answers with a caveat
    "answers_anyway": 0.0,    # answers confidently regardless
}

#: How much of a claimed quote must appear in the passages for the code to accept
#: a `supported` / `present` label. Judges paraphrase and elide; requiring a
#: character-exact substring would downgrade honest verdicts, and requiring
#: nothing would accept invented ones. 0.8 of the quote's length, matched as one
#: contiguous block, is the line: it tolerates an elision or a fixed comma and
#: not a rewritten sentence.
QUOTE_MATCH_RATIO = 0.8

#: Shorter than this and a "quote" is not evidence of anything — "the graph" is
#: in every passage. Below the floor the claim is treated as unbacked.
MIN_QUOTE_CHARS = 24

# --- output caps, and why they are this large --------------------------------
#
# `STRONG` is a reasoning model, and `max_output_tokens` bounds reasoning AND the
# visible reply TOGETHER. Sized for the JSON alone, the cap is spent thinking and
# the verdict never gets written: measured on the first live run, a 1200-token cap
# produced `reasoning_tokens: 1200` and an empty body on the two claim-splitting
# metrics — 2 of 8 verdicts ungradeable for a reason that had nothing to do with
# the judge's ability to judge. Headroom is nearly free (billing is on tokens
# actually emitted, and reasoning is billed either way), so the caps below are set
# from the observed reasoning cost plus room for the answer, not from the size of
# the JSON. A verdict that still hits the cap stays ungradeable rather than being
# read as a finished one.
_CLAIMS_CAP = 3000        # faithfulness / context_recall: ~1000 reasoning observed
_PASSAGE_CAP_BASE = 900   # context_precision: plus 16 per candidate line
_BAND_CAP = 900           # answer_relevance / abstention: one label + a sentence


@dataclass(frozen=True)
class MetricResult:
    """One metric, for one case.

    `score` is `None` whenever the metric is **undefined** for this case (no
    claims to check, no passages, no reference answer) or **ungradeable** (the
    judge truncated or returned nothing parseable). Those are different things
    and `gradeable` tells them apart; both are excluded from averages, and the
    report counts them separately rather than letting either read as 0.0.
    """

    name: str
    score: Optional[float]
    label: str = ""                                   # a named band, where the metric is one
    items: list[dict] = field(default_factory=list)   # per-claim / per-passage verdicts
    downgraded: list[dict] = field(default_factory=list)  # 'supported' with no findable quote
    dropped: list[dict] = field(default_factory=list)     # unusable verdicts, discarded
    gradeable: bool = True
    notes: str = ""
    model: str = ""
    cached: bool = False
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        """USD for this verdict. 0.0 when it was replayed from cache."""
        return 0.0 if self.cached else estimate_cost(self.usage, self.model)


# --- the judged call ----------------------------------------------------------

def _ask(
    kind: str,
    prompt: str,
    system: str,
    *,
    model: str,
    max_output_tokens: int,
    key_parts: Sequence[str],
    use_cache: bool,
) -> tuple[str, dict, bool, bool]:
    """One judge call, cached. Returns `(text, usage, cached, truncated)`.

    Every scorer goes through here so that caching, cost accounting and the
    truncation branch exist once. `key_parts` must contain everything that varies
    the prompt — the cache is what makes these numbers reproducible, and a key
    missing an input is a cache that replays the wrong verdict.
    """
    key = call_key(kind, model, PROMPT_VERSION, list(key_parts))
    if use_cache:
        hit = get_call(key)
        if hit is not None:
            return hit["text"], hit.get("usage") or {}, True, False

    result = call(prompt, system=system, model=model, max_output_tokens=max_output_tokens)
    text = (result.text or "").strip()
    truncated = bool(getattr(result, "truncated", False))

    # A truncated verdict is not stored: it is not a finished judgement, and a
    # cache holding one would replay "ungradeable" forever even after the cap
    # was raised.
    if use_cache and text and not truncated:
        put_call(key, kind, model, text, result.usage)
    return text, result.usage or {}, False, truncated


def _parse_object(text: str) -> Optional[dict]:
    """The JSON object in a judge reply, or None. Never guesses (see `judge.py`)."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _ungradeable(name: str, model: str, why: str, usage: dict) -> MetricResult:
    return MetricResult(
        name=name, score=None, gradeable=False, notes=why, model=model, usage=usage
    )


# --- the deterministic core: is this quote actually in the passages? ----------

def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace. Quote matching must not turn on a newline."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def context_text(chunks: Sequence[Chunk]) -> str:
    """Everything the answer was allowed to use, as one string to check quotes against."""
    return "\n".join(chunk.text for chunk in chunks)


def quote_is_present(quote: str, haystack: str) -> bool:
    """Is `quote` really in the passages?

    Exact first (after whitespace normalisation), then a longest-common-block
    check at `QUOTE_MATCH_RATIO`. The fuzzy arm exists because judges elide with
    "..." and fix punctuation, and rejecting those would report honest verdicts
    as hallucinations. It stays a *contiguous block* rather than a token-overlap
    score, because scattered shared words are exactly what an invented quote
    looks like.
    """
    needle = _normalise(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return False
    hay = _normalise(haystack)
    if needle in hay:
        return True
    match = difflib.SequenceMatcher(None, needle, hay, autojunk=False).find_longest_match(
        0, len(needle), 0, len(hay)
    )
    return match.size >= QUOTE_MATCH_RATIO * len(needle)


def _grade_claims(
    raw_claims: Any,
    haystack: str,
    vocabulary: dict[str, float],
    positive: str,
    negative: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    """`(kept, downgraded, dropped)` for a claim list the judge proposed.

    The one place the code overrules the model: a claim labelled `positive`
    (`supported` / `present`) whose quote cannot be found in `haystack` is
    relabelled `negative` and recorded. `negative` is named by the caller rather
    than picked as "the first zero-valued label" — `contradicted` and
    `unsupported` are both worth 0.0 and mean entirely different things, and a
    downgrade must land on "the passages do not say this", never on "the passages
    say the opposite".

    A claim with a label outside the vocabulary, or with no claim text at all, is
    dropped — an uninterpretable verdict is not evidence in either direction.
    """
    kept: list[dict] = []
    downgraded: list[dict] = []
    dropped: list[dict] = []

    for item in raw_claims or []:
        if not isinstance(item, dict) or not str(item.get("claim", "")).strip():
            dropped.append(item if isinstance(item, dict) else {"raw": item})
            continue
        label = str(item.get("label", "")).strip().lower()
        if label not in vocabulary:
            dropped.append({**item, "note": f"label {label!r} is not one of {sorted(vocabulary)}"})
            continue
        if label == positive and not quote_is_present(str(item.get("quote", "")), haystack):
            downgraded.append({
                **item,
                "note": "quote not found in the passages — downgraded by the harness, "
                        "not by the judge",
            })
            kept.append({**item, "label": negative, "downgraded_from": positive})
            continue
        kept.append(item)

    return kept, downgraded, dropped


def _ratio(kept: Sequence[dict], vocabulary: dict[str, float]) -> Optional[float]:
    """Mean of the label values. `None` on an empty list — undefined, not zero."""
    if not kept:
        return None
    return sum(vocabulary.get(str(c.get("label", "")).lower(), 0.0) for c in kept) / len(kept)


# --- 1. faithfulness ----------------------------------------------------------

_FAITHFULNESS_SYSTEM = """You check whether an ANSWER is grounded in the PASSAGES
it was written from.

Split the answer into atomic factual claims — one fact each, no compound claims.
For every claim decide:
  supported    - the passages state it
  contradicted - the passages state the opposite
  unsupported  - the passages neither state nor contradict it

For a claim you call "supported", copy the sentence from the passages that states
it, VERBATIM. A supported claim whose quote cannot be found in the passages is
counted as unsupported, so never paraphrase a quote.

Judge grounding only. Do not reward the answer for being well written, confident,
detailed or long, and do not use anything you know that the passages do not say.

Return ONE JSON object and nothing else:
{"claims": [{"claim": "<one fact>", "label": "supported|contradicted|unsupported",
             "quote": "<verbatim from the passages, or empty>"}]}
An answer that only says the passages do not cover the question has NO factual
claims: return {"claims": []}."""


def faithfulness(
    answer: str,
    chunks: Sequence[Chunk],
    *,
    model: str = STRONG,
    use_cache: bool = True,
) -> MetricResult:
    """Fraction of the answer's claims the passages actually support.

    `None` when the answer makes no factual claims — a refusal is not unfaithful.
    """
    from .answer import render_context

    haystack = context_text(chunks)
    prompt = f"PASSAGES:\n{render_context(chunks)}\n\nANSWER:\n{answer}"
    text, usage, cached, truncated = _ask(
        "faithfulness", prompt, _FAITHFULNESS_SYSTEM,
        model=model, max_output_tokens=_CLAIMS_CAP,
        key_parts=[answer, *[c.chunk_id for c in chunks]], use_cache=use_cache,
    )
    if truncated:
        return _ungradeable("faithfulness", model, "judge hit the output cap", usage)
    raw = _parse_object(text)
    if raw is None:
        return _ungradeable("faithfulness", model, "judge returned no parseable verdict", usage)

    kept, downgraded, dropped = _grade_claims(
        raw.get("claims"), haystack, CLAIM_LABELS, "supported", "unsupported"
    )
    return MetricResult(
        name="faithfulness",
        score=_ratio(kept, CLAIM_LABELS),
        items=kept,
        downgraded=downgraded,
        dropped=dropped,
        notes="" if kept else "no factual claims in the answer — undefined, not zero",
        model=model, cached=cached, usage=usage,
    )


# --- 2. answer relevance ------------------------------------------------------

_RELEVANCE_SYSTEM = """You decide whether an ANSWER addresses the QUESTION.

  answers           - it directly answers the question that was asked
  partially_answers - it addresses the question but leaves its main part unanswered
  evades            - it answers a different question, or does not address it at all

Judge ONLY whether the question was addressed. Correctness and grounding are
judged elsewhere. Do not reward length, confidence, or extra detail the question
did not ask for; a short exact answer is "answers".

Return ONE JSON object and nothing else:
{"label": "answers|partially_answers|evades",
 "rationale": "<one sentence: what the question asked, what the answer gave>"}
A verdict with no rationale is discarded, so write one."""


def answer_relevance(
    question: str,
    answer: str,
    *,
    model: str = STRONG,
    use_cache: bool = True,
) -> MetricResult:
    """Does the answer address the question. Named band -> {1.0, 0.5, 0.0}.

    The rationale is required for the same reason `judge.py` requires one: a
    label nobody can check is not a measurement. An unbacked label is discarded
    and the case is reported ungradeable rather than scored on the judge's word.
    """
    prompt = f"QUESTION:\n{question}\n\nANSWER:\n{answer}"
    text, usage, cached, truncated = _ask(
        "answer_relevance", prompt, _RELEVANCE_SYSTEM,
        model=model, max_output_tokens=_BAND_CAP,
        key_parts=[question, answer], use_cache=use_cache,
    )
    if truncated:
        return _ungradeable("answer_relevance", model, "judge hit the output cap", usage)
    raw = _parse_object(text)
    if raw is None:
        return _ungradeable("answer_relevance", model, "judge returned no parseable verdict", usage)

    label = str(raw.get("label", "")).strip().lower()
    rationale = str(raw.get("rationale", "")).strip()
    if label not in RELEVANCE_BANDS:
        return _ungradeable("answer_relevance", model, f"label {label!r} is not a band", usage)
    if not rationale:
        return MetricResult(
            name="answer_relevance", score=None, label=label, gradeable=False,
            dropped=[{"label": label, "note": "no rationale — verdict discarded"}],
            notes="unbacked verdict discarded", model=model, cached=cached, usage=usage,
        )

    return MetricResult(
        name="answer_relevance",
        score=RELEVANCE_BANDS[label],
        label=label,
        items=[{"label": label, "rationale": rationale}],
        model=model, cached=cached, usage=usage,
    )


# --- 3. context precision -----------------------------------------------------

_PRECISION_SYSTEM = """You decide which retrieved PASSAGES are useful for a QUESTION.

For EACH numbered passage:
  relevant   - it contains information needed to answer the question
  irrelevant - it does not, even if it shares words or names with the question

A passage is not relevant merely because it mentions the same identifiers. The
passages are in no particular order; judge each on its own content.

Reply with ONE line per passage and nothing else:
<number>: <relevant|irrelevant>"""

_PASSAGE_LINE = re.compile(r"(\d+)\s*[:.\)-]\s*(relevant|irrelevant)", re.I)


def _shuffled_order(question: str, n: int) -> list[int]:
    """A deterministic permutation of `range(n)`, seeded from the question.

    Deterministic because the cache keys on the rendered prompt: a fresh shuffle
    every run would miss the cache every run and the tables would stop
    reproducing. Shuffled because the judge is being asked about passages the
    retriever already ordered, and grading a ranked list top-down invites the
    order itself to carry the verdict.
    """
    seed = int(hashlib.sha256(question.encode("utf-8")).hexdigest()[:8], 16)
    order = list(range(n))
    random.Random(seed).shuffle(order)
    return order


def context_precision(
    question: str,
    chunks: Sequence[Chunk],
    *,
    model: str = STRONG,
    use_cache: bool = True,
) -> MetricResult:
    """Fraction of the retrieved passages a judge calls relevant.

    Deliberately **not** Part 2's `precision@k`: that one asks whether a chunk is
    in the golden set, this one asks a model whether it helps. They can disagree,
    and where they do it is a finding about the golden set rather than a bug in
    either — a chunk nobody anchored can still answer the question.
    """
    if not chunks:
        return MetricResult(
            name="context_precision", score=None, model=model,
            notes="no passages retrieved — undefined, not zero",
        )

    order = _shuffled_order(question, len(chunks))
    shown = [chunks[i] for i in order]
    listing = "\n\n".join(
        f"[{position}] {chunk.heading_path or chunk.symbol_uid or chunk.source_path}\n{chunk.text}"
        for position, chunk in enumerate(shown, start=1)
    )
    text, usage, cached, truncated = _ask(
        "context_precision", f"QUESTION: {question}\n\nPASSAGES:\n{listing}", _PRECISION_SYSTEM,
        model=model, max_output_tokens=_PASSAGE_CAP_BASE + 16 * len(chunks),
        key_parts=[question, *[c.chunk_id for c in shown]], use_cache=use_cache,
    )
    if truncated:
        return _ungradeable("context_precision", model, "judge hit the output cap", usage)

    verdicts: dict[int, str] = {}
    for match in _PASSAGE_LINE.finditer(text or ""):
        position = int(match.group(1))
        if 1 <= position <= len(shown):
            verdicts[position] = match.group(2).lower()
    if not verdicts:
        return _ungradeable("context_precision", model, "no parseable per-passage verdicts", usage)

    # Map back out of the shuffle, so `items` is reported in RETRIEVER order and
    # can be read next to Part 2's ranked list.
    items: list[dict] = []
    dropped: list[dict] = []
    for shown_position, original_index in enumerate(order, start=1):
        label = verdicts.get(shown_position)
        entry = {
            "rank": original_index + 1,
            "chunk_id": shown[shown_position - 1].chunk_id,
            "label": label,
            "shown_at": shown_position,
        }
        # An unjudged passage is dropped, not counted as irrelevant: a judge that
        # skipped a line has said nothing about it, and reading silence as a
        # verdict is how a parser bug becomes a metric.
        (items if label in PASSAGE_LABELS else dropped).append(entry)
    items.sort(key=lambda entry: entry["rank"])

    return MetricResult(
        name="context_precision",
        score=_ratio(items, PASSAGE_LABELS),
        items=items,
        dropped=dropped,
        notes="" if not dropped else f"{len(dropped)} passage(s) unjudged and excluded",
        model=model, cached=cached, usage=usage,
    )


# --- 4. context recall --------------------------------------------------------

_RECALL_SYSTEM = """You check whether retrieved PASSAGES contain what a REFERENCE
ANSWER says.

Split the reference answer into atomic claims — one fact each. For every claim:
  present - the passages state it; quote the sentence that does, VERBATIM
  absent  - the passages do not state it

A claim called "present" whose quote cannot be found in the passages is counted
as absent, so never paraphrase a quote. Judge only what the passages contain; do
not use outside knowledge.

Return ONE JSON object and nothing else:
{"claims": [{"claim": "<one fact>", "label": "present|absent",
             "quote": "<verbatim from the passages, or empty>"}]}"""


def context_recall(
    golden_answer: str,
    chunks: Sequence[Chunk],
    *,
    model: str = STRONG,
    use_cache: bool = True,
) -> MetricResult:
    """Fraction of the reference answer's claims the retrieved passages carry.

    `None` when there is no reference answer — which is precisely the
    out-of-corpus case, where there is nothing to have retrieved.
    """
    from .answer import render_context

    if not (golden_answer or "").strip():
        return MetricResult(
            name="context_recall", score=None, model=model,
            notes="no reference answer — undefined (out-of-corpus), not zero",
        )
    if not chunks:
        return MetricResult(
            name="context_recall", score=0.0, model=model,
            notes="nothing retrieved: no reference claim can be covered",
        )

    haystack = context_text(chunks)
    prompt = f"PASSAGES:\n{render_context(chunks)}\n\nREFERENCE ANSWER:\n{golden_answer}"
    text, usage, cached, truncated = _ask(
        "context_recall", prompt, _RECALL_SYSTEM,
        model=model, max_output_tokens=_CLAIMS_CAP,
        key_parts=[golden_answer, *[c.chunk_id for c in chunks]], use_cache=use_cache,
    )
    if truncated:
        return _ungradeable("context_recall", model, "judge hit the output cap", usage)
    raw = _parse_object(text)
    if raw is None:
        return _ungradeable("context_recall", model, "judge returned no parseable verdict", usage)

    kept, downgraded, dropped = _grade_claims(
        raw.get("claims"), haystack, COVERAGE_LABELS, "present", "absent"
    )
    return MetricResult(
        name="context_recall",
        score=_ratio(kept, COVERAGE_LABELS),
        items=kept,
        downgraded=downgraded,
        dropped=dropped,
        notes="" if kept else "no claims extracted from the reference answer",
        model=model, cached=cached, usage=usage,
    )


# --- the out-of-corpus metric -------------------------------------------------

_ABSTENTION_SYSTEM = """An ANSWER was produced for a QUESTION the source material
does not cover. Decide what the answer did:

  abstains       - it states that the passages/documents do not contain the answer
  hedges         - it gives an answer but flags that the passages may not cover it
  answers_anyway - it answers as though the material supported it

Return ONE JSON object and nothing else:
{"label": "abstains|hedges|answers_anyway", "rationale": "<one sentence>"}"""


def abstention(
    question: str,
    answer: str,
    *,
    model: str = STRONG,
    use_cache: bool = True,
) -> MetricResult:
    """Out-of-corpus cases only: did the answer refuse, hedge, or hallucinate?

    Reported on its own, never averaged into the four. The four measure how well
    a question was answered; on a question the corpus cannot answer, answering it
    well is the failure. Part 1 found retrieval returns confident junk for these
    at every stage — this is the number that says whether the generator passed
    that junk on to the user.
    """
    prompt = f"QUESTION:\n{question}\n\nANSWER:\n{answer}"
    text, usage, cached, truncated = _ask(
        "abstention", prompt, _ABSTENTION_SYSTEM,
        model=model, max_output_tokens=_BAND_CAP,
        key_parts=[question, answer], use_cache=use_cache,
    )
    if truncated:
        return _ungradeable("abstention", model, "judge hit the output cap", usage)
    raw = _parse_object(text)
    if raw is None:
        return _ungradeable("abstention", model, "judge returned no parseable verdict", usage)

    label = str(raw.get("label", "")).strip().lower()
    if label not in ABSTENTION_BANDS:
        return _ungradeable("abstention", model, f"label {label!r} is not a band", usage)
    return MetricResult(
        name="abstention",
        score=ABSTENTION_BANDS[label],
        label=label,
        items=[{"label": label, "rationale": str(raw.get("rationale", "")).strip()}],
        model=model, cached=cached, usage=usage,
    )


# --- one case, and the aggregate ---------------------------------------------

def score_case(
    question: str,
    answer: str,
    chunks: Sequence[Chunk],
    golden_answer: str,
    *,
    out_of_corpus: bool = False,
    model: str = STRONG,
    use_cache: bool = True,
) -> dict[str, MetricResult]:
    """Every applicable metric for one case.

    Out-of-corpus cases run **`abstention` only**. Faithfulness would be
    undefined (a refusal makes no claims), context recall has no reference to
    recall, and relevance would score a correct refusal as `evades` — three
    numbers that would say nothing while dragging three averages down. Running
    the applicable metric instead of running four and discarding them also means
    those cases cost one judged call rather than four.
    """
    if out_of_corpus:
        return {"abstention": abstention(question, answer, model=model, use_cache=use_cache)}
    return {
        "faithfulness": faithfulness(answer, chunks, model=model, use_cache=use_cache),
        "answer_relevance": answer_relevance(question, answer, model=model, use_cache=use_cache),
        "context_precision": context_precision(question, chunks, model=model, use_cache=use_cache),
        "context_recall": context_recall(golden_answer, chunks, model=model, use_cache=use_cache),
    }


def aggregate(per_case: Sequence[dict[str, MetricResult]]) -> dict[str, Optional[float]]:
    """Macro-average each metric over cases, skipping `None` (undefined/ungradeable).

    Uses Part 2's `mean` deliberately: one definition of "skip the undefined"
    across both halves of the harness, so a reader comparing the two tables is
    not comparing two averaging conventions (decision #67).
    """
    return {
        name: mean(case[name].score for case in per_case if name in case)
        for name in METRIC_NAMES
    }


def total_cost(per_case: Sequence[dict[str, MetricResult]]) -> float:
    """USD actually spent on judged verdicts — cached replays cost nothing."""
    return sum(result.cost for case in per_case for result in case.values())


__all__ = [
    "METRIC_NAMES",
    "PROMPT_VERSION",
    "CLAIM_LABELS",
    "COVERAGE_LABELS",
    "RELEVANCE_BANDS",
    "PASSAGE_LABELS",
    "ABSTENTION_BANDS",
    "MetricResult",
    "context_text",
    "quote_is_present",
    "faithfulness",
    "answer_relevance",
    "context_precision",
    "context_recall",
    "abstention",
    "score_case",
    "aggregate",
    "total_cost",
]
