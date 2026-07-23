"""main.py — RADF HW1 CLI entry point.

Owner: Berat (T0.9). Loads `.env`, assembles the tool registry, and runs one
query through the agent loop with an input()-based approval gate.

Usage:
    python main.py "which components import agentlib.core?"
    python main.py --model strong --max-steps 6 "list the components of this repo"

Phase 0 definition of done: this runs the full observe -> reason -> act -> verify
loop and fails only with NotImplementedError from the (unimplemented) tool stubs.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from agentlib.core import CHEAP, STRONG, estimate_cost
from agentlib.loop import run_agent
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
    args = parser.parse_args(argv)

    # Load .env so OPENCODE_API_KEY (and optional model-id overrides) are present.
    load_dotenv()

    model = STRONG if args.model == "strong" else CHEAP
    schemas, registry = build_registry()

    try:
        result = run_agent(
            args.query,
            schemas,
            registry,
            approve=approve_via_input,
            model=model,
            max_steps=args.max_steps,
            verbose=not args.quiet,
        )
    except NotImplementedError as e:
        # Expected in Phase 0: the loop reached a tool stub. Report it honestly.
        print(f"\n[stub] reached an unimplemented tool: {e}", file=sys.stderr)
        return 2

    print("\n" + "=" * 60)
    print("stopped:", result["stopped"], "| steps:", result["steps"])
    print("answer :", result["answer"])
    if result["trace"]:
        print("trace  :")
        for ev in result["trace"]:
            print(f"    [{ev['branch']}] {ev['tool']}({ev['args']}) -> {ev['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
