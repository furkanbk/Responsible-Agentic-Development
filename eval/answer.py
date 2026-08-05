"""eval.answer — the answer under test.

Owner: Dias Sarkytbaev (HW5, T14.14).

Part 2 scores the ranking. Part 3 scores *an answer built on that ranking*, so
the harness needs one, and this is it: the retrieved passages, one CHEAP call,
and a short answer. It is deliberately the smallest thing that can be judged —
no tool loop, no re-query, no agent state. Agent-level evaluation is HW3's
subject, not this one, and a generation metric measured over a multi-step agent
run cannot tell a retrieval problem from a planning problem.

Three choices here decide what the judged numbers can mean:

**The passages go in in RETRIEVER order, not repacked.** `pack_for_llm` exists
and the agent may use it (decision #59), but Part 2 scored the retriever's order
and Part 3 has to score an answer built on *the same* context, or a
Part-2-versus-Part-3 disagreement could be blamed on the repack instead of on
the generator. Repacking is a downstream choice; leaving it out of the measured
path is the same rule the retrieval metrics follow.

**The generator is told it may refuse.** The out-of-corpus cases only measure
something if abstaining is available: a model that has never been told it can
say "the passages do not cover this" is being scored on a behaviour nobody
offered it. So the instruction says it explicitly — and the abstention number in
the report is then about the model's judgement, not about the prompt's omission.

**The answer is capped at ~120 words.** Verbosity is a documented LLM-as-judge
bias: longer answers score better for reasons unrelated to being right. The cap
puts every candidate in the same size band before the judge sees it, and the
report carries the measured mean length so the mitigation is checkable rather
than asserted. (The faithfulness scorer is claim-normalised as well, so padding
dilutes it rather than inflating it — two independent defences, since a prompt
cap is a request and not a guarantee.)

Answers are cached (`eval.cache`) on the model, prompt version, question and the
exact context ids. Without that, re-running the harness re-samples every answer
and the tables move for reasons that have nothing to do with the change being
measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from agentlib.core import CHEAP, call, estimate_cost
from retrieval.types import Chunk

from .cache import call_key, get_call, put_call

#: Bump when `_INSTRUCTION` or the passage rendering changes — see `call_key`.
PROMPT_VERSION = "answer-v1"

#: The word cap requested of the generator (verbosity bias, above).
MAX_WORDS = 120

#: Output-token ceiling. ~120 words is ~180 tokens; 400 leaves room to finish a
#: sentence, and a reply that still hits the cap is reported, never trimmed and
#: passed off as a finished answer (`agentlib.core.Result.truncated`).
MAX_OUTPUT_TOKENS = 400

_INSTRUCTION = f"""You answer questions about a software project, using ONLY the
numbered passages provided.

Rules:
- Use only what the passages say. Do not add knowledge from anywhere else.
- If the passages do not contain the answer, say exactly that and stop. Do not
  guess, and do not answer from general knowledge about software.
- Answer in at most {MAX_WORDS} words. No preamble, no bullet lists, no headings.
"""


@dataclass(frozen=True)
class Answer:
    """One generated answer plus everything needed to judge and price it."""

    text: str
    model: str
    context_ids: tuple[str, ...]          # the chunks that were in front of it
    cached: bool = False
    truncated: bool = False               # hit the output cap — NOT a finished answer
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        return estimate_cost(self.usage, self.model)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def render_context(chunks: Sequence[Chunk]) -> str:
    """The passages as the generator sees them: numbered, heading path first.

    The heading path is what makes a passage self-describing — a chunk reading
    "JSON, because it is diffable" is unusable without it (contract #10) — and
    the numbering is what lets a judged verdict point at one passage.
    """
    return "\n\n".join(
        f"[{i}] {chunk.heading_path or chunk.symbol_uid or chunk.source_path}\n{chunk.text}"
        for i, chunk in enumerate(chunks, start=1)
    )


def generate_answer(
    question: str,
    chunks: Sequence[Chunk],
    *,
    model: str = CHEAP,
    use_cache: bool = True,
) -> Answer:
    """Answer `question` from `chunks` alone. Cached on (model, prompt, context).

    An empty context is not an error: it is the honest input for a query that
    retrieved nothing, and the generator should refuse rather than fall back on
    what it happens to know.
    """
    context_ids = tuple(c.chunk_id for c in chunks)
    prompt = f"QUESTION: {question}\n\nPASSAGES:\n{render_context(chunks)}"
    key = call_key("answer", model, PROMPT_VERSION, [question, *context_ids])

    if use_cache:
        hit = get_call(key)
        if hit is not None:
            return Answer(
                text=hit["text"],
                model=model,
                context_ids=context_ids,
                cached=True,
                usage=hit.get("usage") or {},
            )

    result = call(
        prompt,
        system=_INSTRUCTION,
        model=model,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    text = (result.text or "").strip()

    # A truncated reply is stored like any other — it is what the model actually
    # produced, and hiding it would make the run unreproducible. The flag travels
    # with it so the report can count truncations instead of scoring them silently.
    if use_cache and text:
        put_call(key, "answer", model, text, result.usage)

    return Answer(
        text=text,
        model=model,
        context_ids=context_ids,
        cached=False,
        truncated=bool(getattr(result, "truncated", False)),
        usage=result.usage or {},
    )


def answer_stats(answers: Sequence[Answer]) -> dict[str, Any]:
    """Mean length, cache hits, truncations, spend — the honesty line in the report."""
    if not answers:
        return {"count": 0, "mean_words": None, "cached": 0, "truncated": 0, "cost": 0.0}
    return {
        "count": len(answers),
        "mean_words": sum(a.word_count for a in answers) / len(answers),
        "max_words": max(a.word_count for a in answers),
        "cached": sum(1 for a in answers if a.cached),
        "truncated": sum(1 for a in answers if a.truncated),
        "cost": sum(a.cost for a in answers),
    }


__all__ = [
    "MAX_WORDS",
    "PROMPT_VERSION",
    "Answer",
    "render_context",
    "generate_answer",
    "answer_stats",
]
