"""demo_fact_cue — a saved fact resurfaces on its cue and changes the answer.

Owner: Berat Furkan Kocak (HW2, T8.1).
Run:  python -m demos.demo_fact_cue

An A/B on the same question. Run once with an empty memory store, once with one
fact saved — nothing else differs. The fact is never mentioned in the question;
it comes back because its CUE matched, which is the whole reason a cue is
required at save time.

What the agent does with the fact once it surfaces is the model's call. That is
the difference from a rule: a fact is information, and information has to be
interpreted. A rule would already say what to do.
"""

from __future__ import annotations

from demos._harness import (cleanup, have_live_model, header, isolate_stores,
                            step, verdict)

QUESTION = "We want to cut costs — should we switch the cheap model to gemini-3-flash?"

FACT = ("Gemini model ids are listed by Zen but 400 on every request — Zen "
        "forwards our OpenAI-shaped body untranslated. Verified on "
        "gemini-3-flash and gemini-3.5-flash-lite; gpt-5-nano works on the "
        "identical code path.")


def ask(question: str) -> tuple[str, list[str]]:
    """Assemble context for `question` and ask the model. Returns (answer, cues)."""
    from agentlib.context import assemble
    from agentlib.core import CHEAP, call
    from agentlib.session import session_scope

    with session_scope("berat") as s:
        ctx = assemble(base_system="You are a codebase knowledge-graph agent. "
                                   "Answer in at most four sentences.",
                       query=question, session=s)
    pulled = [b for b in ctx.data_blocks]
    result = call(messages=ctx.input_items(question), system=ctx.instructions,
                  model=CHEAP)
    return (result.text or "").strip(), pulled


def main() -> int:
    box = isolate_stores()
    try:
        from agentlib.session import session_scope
        from overlay import memory as mem
        from tools.memory_tools import save_memory

        header("A saved fact resurfaces on its cue",
               "§2 — a fact comes back on its cue and the agent acts on it "
               "without being told")

        if not have_live_model():
            print("  OPENCODE_API_KEY is not set — this demo needs a live model "
                  "to show a behaviour change.\n  Set it in .env and re-run.")
            return 2

        step("A — memory is empty. Ask the question.")
        before, _ = ask(QUESTION)
        print(f"  {before}")

        step("Berat mentions the Gemini problem in passing; the agent saves it")
        with session_scope("berat"):
            saved = save_memory(
                FACT, kind="fact",
                cue="gemini,model,cheap,zen,switch,cost",
                visibility="team", stated_by_user=True,
            )
        print(f"  saved {saved['memory_id']} cue={saved['cue']}")

        step("B — the SAME question, nothing else changed")
        after, pulled = ask(QUESTION)
        print(f"  {after}")

        step("What made it come back")
        hits = mem.retrieve_memory(QUESTION, user_id="berat")
        print(f"  retrieve_memory({QUESTION[:40]!r}...) matched "
              f"{len(hits)} memory on cue")
        for h in hits:
            matched = [c for c in h["cue"] if c in QUESTION.lower()]
            print(f"    cue terms present in the question: {matched}")

        ok = True
        ok &= verdict(bool(pulled), "the fact was pulled into context on cue "
                                    "alone — the question never named it")
        ok &= verdict("gemini" in after.lower() and
                      any(w in after.lower() for w in ("400", "fail", "not work",
                                                       "doesn't work", "unsupported",
                                                       "broken", "untranslated")),
                      "the answer now warns about Gemini")
        ok &= verdict(after.strip() != before.strip(),
                      "the answer changed, and the only difference was one "
                      "saved fact")

        print("\n  Note: the agent DECIDED what to do with the fact — it was "
              "information,\n  not an instruction. A rule would have said what "
              "to do (see demo_rule_unprompted).")
        return 0 if ok else 1
    finally:
        cleanup(box)


if __name__ == "__main__":
    raise SystemExit(main())
