"""main.py — RADF CLI entry point.

Owner: Berat (T0.9, extended in HW2 T5.2). Loads `.env`, assembles the tool
registry, and runs one query through the agent loop with an input()-based
approval gate.

Usage:
    python main.py "which components import agentlib.core?"
    python main.py --user berat "can I lift the _load_graph helpers out?"
    python main.py --user dias --thread review-42 --model strong "..."

`--user` is the identity the runtime asserts. Memory and decisions are scoped to
it, and everything the agent records is attributed to it. In a real deployment
this comes from authentication, not from a flag — the point is that it comes
from the runtime, never from the model.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from agentlib.context import assemble
from agentlib.core import CHEAP, STRONG, estimate_cost
from agentlib.loop import DEFAULT_SYSTEM, run_agent
from agentlib.runlog import RunLog
from agentlib.session import session_scope
from tools import build_registry


def approve_via_input(name: str, args: dict) -> bool:
    """Approval callback for gated (irreversible) tools — the real REPL gate.

    Prints what the agent wants to do and reads a y/n from the human. Anything
    other than an explicit 'y' declines (fail-safe: default is do-not-run).
    """
    answer = input(f"    Approve irreversible {name}({args})? [y/N] ").strip().lower()
    return answer == "y"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RADF HW1 — one agent over the knowledge-graph tools."
    )
    parser.add_argument("query", help="the request for the agent")
    parser.add_argument(
        "--model", choices=["cheap", "strong"], default="cheap",
        help="which pinned model to use (default: cheap)",
    )
    parser.add_argument(
        "--max-steps", type=int, default=8,
        help="loop step ceiling (default: 8)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress per-step trace output",
    )
    parser.add_argument(
        "--user", default="anonymous",
        help="the acting user id — scopes memory and attributes what is recorded",
    )
    parser.add_argument(
        "--thread", default="default",
        help="thread id within this user's session (default: default)",
    )
    parser.add_argument(
        "--component", default="",
        help="component the request is about; seeds the impact set so module "
             "rules and decisions bind without a planner round",
    )
    args = parser.parse_args(argv)

    # Load .env so OPENCODE_API_KEY (and optional model-id overrides) are present.
    load_dotenv()

    model = STRONG if args.model == "strong" else CHEAP
    schemas, registry = build_registry()

    with session_scope(args.user, args.thread) as session:
        from overlay.uid import resolve_uid

        impact = [uid] if (uid := resolve_uid(args.component)) else []
        context = assemble(
            base_system=DEFAULT_SYSTEM,
            query=args.query,
            impact=impact,
            session=session,
        )
        run_log = RunLog(
            agent="cli", user_id=session.user_id, thread_id=session.thread_id,
            request=args.query,
        )
        if not args.quiet:
            print(f"  [SESSION] {session} | pushed: {context.sources['pushed']}")
            print(f"  [CONTEXT] {len(context.data_blocks)} quoted block(s) pulled")

        result = run_agent(
            args.query,
            schemas,
            registry,
            approve=approve_via_input,
            model=model,
            max_steps=args.max_steps,
            verbose=not args.quiet,
            context=context,
            run_log=run_log,
        )

    print("\n" + "=" * 60)
    print("stopped:", result["stopped"], "| steps:", result["steps"])
    print("answer :", result["answer"])
    if result["trace"]:
        print("trace  :")
        for ev in result["trace"]:
            print(f"    [{ev['branch']}] {ev['tool']}({ev['args']}) -> {ev['output']}")
    print("run_id :", result.get("run_id"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
