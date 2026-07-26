"""demo_rule_unprompted — a rule changes behaviour the user never asked for.

Owner: Berat Furkan Kocak (HW2, T8.1).
Run:  python -m demos.demo_rule_unprompted

The contrast with `demo_fact_cue` is the point of the whole fact/rule split:

    a FACT is information. It surfaces on a cue, and the MODEL decides what to
    do with it.

    a RULE already says what to do. The model does not interpret it; it only
    has to be in force at the right moment. And mostly the model does not even
    decide that — a rule with `applies_to` set is bound MECHANICALLY by the
    impact set, with no model call involved.

Both halves are shown: an always-on rule from memory, and a module-bound rule
from a file that arrives because the graph routed it.
"""

from __future__ import annotations

from demos._harness import (cleanup, have_live_model, header, isolate_stores,
                            step, verdict)

QUESTION = "I want to add a retry counter to the decisions tool. How should I go about it?"

RULE = ("Before suggesting any edit to a file, state which teammate owns it "
        "according to the TODO.md ownership map, and say that a change to a "
        "file you do not own needs their agreement first.")


def ask(question: str, impact: list[str] | None = None) -> tuple[str, list, list]:
    from agentlib.context import assemble
    from agentlib.core import CHEAP, call
    from agentlib.session import session_scope

    with session_scope("berat") as s:
        ctx = assemble(base_system="You are a codebase knowledge-graph agent. "
                                   "Answer in at most five sentences.",
                       query=question, impact=impact or [], session=s)
    result = call(messages=ctx.input_items(question), system=ctx.instructions,
                  model=CHEAP)
    return (result.text or "").strip(), ctx.data_blocks, ctx.sources["pushed"]


def main() -> int:
    box = isolate_stores()
    try:
        from agentlib.session import session_scope
        from tools.memory_tools import save_memory

        header("A rule changes behaviour without being mentioned",
               "§2 — a rule is attached always, and the model decides only "
               "WHEN it applies")

        if not have_live_model():
            print("  OPENCODE_API_KEY is not set — this demo needs a live model.")
            return 2

        step("A — no rule saved. Ask the question.")
        before, _, _ = ask(QUESTION)
        print(f"  {before}")

        step("The team lead records a standing rule — once, not per request")
        with session_scope("berat"):
            saved = save_memory(RULE, kind="rule", cue="", visibility="team",
                                stated_by_user=True)
        print(f"  saved {saved['memory_id']} kind={saved['kind']} "
              f"cue={saved['cue'] or '(none — a standing rule needs no cue)'}")

        step("B — the SAME question. The user never mentions ownership.")
        after, blocks, _ = ask(QUESTION)
        print(f"  {after}")

        ok = True
        ok &= verdict(any("quoted-memory" in b and "kind=\"rule\"" in b
                          for b in blocks),
                      "the rule was attached with no cue match — an unbound "
                      "rule is always a candidate")
        owner_words = ("own", "dias", "ownership", "agreement", "todo.md")
        ok &= verdict(any(w in after.lower() for w in owner_words),
                      "the answer now raises ownership, which the user never "
                      "asked about")
        ok &= verdict(not any(w in before.lower() for w in ("dias", "ownership")),
                      "and did not before — the rule is what changed it")

        step("The mechanical half: a MODULE-bound rule, routed by the graph")
        _, _, pushed_without = ask("what should I keep in mind?", impact=[])
        _, _, pushed_with = ask("what should I keep in mind?",
                                impact=["Module:tools.decisions"])
        print(f"  impact=[]                        -> pushed: {pushed_without}")
        print(f"  impact=['Module:tools.decisions'] -> pushed: {pushed_with}")
        ok &= verdict("rules/modules/tools.md" in pushed_with
                      and "rules/modules/tools.md" not in pushed_without,
                      "the module rule bound off the impact set alone — no "
                      "model call decided it was relevant")

        print("\n  The rule said WHAT. The graph and the cue said WHEN. The "
              "model only picked\n  among candidates that were already "
              "narrowed — so a misapplied rule traces to a\n  wrong impact set "
              "or a wrong cue, never to model judgement.")
        return 0 if ok else 1
    finally:
        cleanup(box)


if __name__ == "__main__":
    raise SystemExit(main())
