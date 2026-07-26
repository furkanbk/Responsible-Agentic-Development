"""demo_private — a private fact saved by A never surfaces for B.

Owner: Berat Furkan Kocak (HW2, T8.2).
Run:  python -m demos.demo_private

The assertion is on the ASSEMBLED CONTEXT, not on an answer. If A's fact is
never in B's context, no sampling of B's model can leak it. If it *were* in the
context and we relied on the model to ignore it, one green run would prove
nothing — the failure would be rare, not absent.
"""

from __future__ import annotations

import sys

from demos._harness import cleanup, header, isolate_stores, show_context, step, verdict


def main() -> int:
    box = isolate_stores()
    try:
        from agentlib.context import assemble
        from agentlib.session import session_scope
        from tools.memory_tools import save_memory

        header("Private memory stays private",
               "§3 — a fact saved for user A never surfaces for user B")

        step("Alice tells her agent something personal")
        with session_scope("alice"):
            saved = save_memory(
                "Only has budget for one paid API tier this quarter",
                kind="fact", cue="cost,budget,pricing,model",
                visibility="private", stated_by_user=True,
            )
        print(f"  saved {saved['memory_id']} visibility={saved['visibility']} "
              f"status={saved['status']}")

        question = "which model should we use, given cost?"

        step(f"Alice asks: {question!r}")
        with session_scope("alice") as s:
            alice_ctx = assemble(base_system="(system)", query=question, session=s)
        show_context("Alice's assembled context:", alice_ctx)

        step(f"Bob asks the SAME question: {question!r}")
        with session_scope("bob") as s:
            bob_ctx = assemble(base_system="(system)", query=question, session=s)
        show_context("Bob's assembled context:", bob_ctx)

        alice_blob = "\n".join(alice_ctx.data_blocks)
        bob_blob = "\n".join(bob_ctx.data_blocks) + bob_ctx.instructions

        ok = True
        ok &= verdict("budget for one paid API tier" in alice_blob,
                      "Alice's own fact is present for Alice")
        ok &= verdict("budget" not in bob_blob.lower(),
                      "Alice's fact is ABSENT from Bob's context entirely — "
                      "not filtered out downstream, never assembled")
        ok &= verdict(alice_ctx.instructions == bob_ctx.instructions.replace("bob", "alice"),
                      "both users get the same pushed rules — scoping affects "
                      "memory, not policy")

        print("\n  Where this is enforced: overlay/memory.py::_visible and "
              "overlay/db.py::visible_to,\n  applied in the query before any "
              "text reaches a model. Not a prompt instruction.")
        return 0 if ok else 1
    finally:
        cleanup(box)


if __name__ == "__main__":
    raise SystemExit(main())
