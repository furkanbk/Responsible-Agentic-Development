"""demo_shared — a decision A records reaches B's agent unprompted.

Owner: Berat Furkan Kocak (HW2, T8.2).
Run:  python -m demos.demo_shared

This is the case the whole project is for. Alejandro records *why* the private
I/O helpers live where they do. Weeks later Dias asks whether he can move them,
never having seen that decision — and his agent has it, because the decision was
attached to the component and the graph routed it in.

Note what Dias does NOT have to do: know the decision exists, know who wrote it,
or search for it. He asks about a module; the constraint arrives with it.
"""

from __future__ import annotations

from demos._harness import cleanup, header, isolate_stores, show_context, step, verdict


def main() -> int:
    box = isolate_stores()
    try:
        from agentlib.context import assemble
        from agentlib.session import session_scope
        from tools.decisions import append_decision_record, retrieve_decisions

        header("Shared knowledge reaches every engineer's agent",
               "§3 — content one user creates, another user's agent sees")

        step("Alejandro records a decision while working on tools/decisions.py")
        with session_scope("alejandro"):
            rec = append_decision_record(
                component="tools/decisions.py",
                decision="Keep the _-private graph I/O helpers in tools/decisions.py",
                rationale=("Phase 1 modules import them across the package "
                           "boundary. Lifting them into a shared module is a "
                           "contract change, not a cleanup — agree it first."),
                status="accepted",
                visibility="team",
            )
        print(f"  {rec['decision_id']}  by={rec['author_id']}  "
              f"uid={rec['symbol_uid']}  visibility={rec['visibility']}")

        step("Weeks later, Dias asks about the same module — he has never seen "
             "that decision")
        question = "can I move the _load_graph helpers into a shared module?"
        with session_scope("dias") as s:
            ctx = assemble(base_system="(system)", query=question,
                           impact=["Module:tools.decisions"], session=s)
        show_context("Dias's assembled context:", ctx)

        blob = "\n".join(ctx.data_blocks)
        ok = True
        ok &= verdict("contract change, not a cleanup" in blob,
                      "Alejandro's rationale is in Dias's context, unprompted")
        ok &= verdict('author="alejandro"' in blob,
                      "it is attributed — Dias can see whose decision it is")
        ok &= verdict("rules/modules/tools.md" in ctx.sources["pushed"],
                      "the module's rule file bound mechanically off the impact "
                      "set — no model call decided it was relevant")

        step("The same lookup as a tool call, which is how the agent pulls it "
             "mid-run")
        with session_scope("dias"):
            out = retrieve_decisions("tools/decisions.py")
        print(f"  retrieve_decisions -> {out['count']} decision(s) for "
              f"{out['symbol_uid']}")
        ok &= verdict(out["count"] >= 1, "the pull path returns it too")

        step("A different spelling of the same component resolves identically")
        with session_scope("dias"):
            dotted = retrieve_decisions("tools.decisions")
        ok &= verdict(dotted["symbol_uid"] == out["symbol_uid"],
                      "'tools/decisions.py' and 'tools.decisions' are one uid — "
                      "this is what makes the GitNexus swap a remap")

        return 0 if ok else 1
    finally:
        cleanup(box)


if __name__ == "__main__":
    raise SystemExit(main())
