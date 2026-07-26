"""demos._harness — shared setup for the HW2 demos.

Owner: Berat Furkan Kocak (HW2, T8.1 / T8.2).

Every demo runs against a throwaway store, so running one never touches your
real overlay and running it twice gives the same answer.

**Two of the demos do not call a model at all, and that is the stronger proof.**
"A did not leak into B's answer" is one sample from a distribution; "A was never
in B's context" is a property. Where a claim can be made about the assembled
context, these demos assert on the context.

The two demos that genuinely need a model — a fact resurfacing on its cue, a
rule changing behaviour — run live if `OPENCODE_API_KEY` is set and fall back to
a scripted model otherwise, saying which mode they used.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Optional

from dotenv import load_dotenv

RULE = "=" * 72


def isolate_stores() -> str:
    """Point all four stores at a fresh temp directory. Returns the path."""
    box = tempfile.mkdtemp(prefix="radf-demo-")
    os.environ["RADF_GRAPH_PATH"] = os.path.join(box, "knowledge_graph.json")
    os.environ["RADF_DB_PATH"] = os.path.join(box, "radf.db")
    os.environ["RADF_MEMORY_PATH"] = os.path.join(box, "memory.json")
    os.environ["RADF_RUNS_DIR"] = os.path.join(box, "runs")
    return box


def cleanup(box: str) -> None:
    shutil.rmtree(box, ignore_errors=True)


def have_live_model() -> bool:
    load_dotenv()
    return bool(os.environ.get("OPENCODE_API_KEY"))


def header(title: str, requirement: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")
    print(f"HW2 requirement: {requirement}\n")


def step(text: str) -> None:
    print(f"\n--- {text} ---")


def verdict(passed: bool, claim: str) -> bool:
    print(f"\n  [{'PASS' if passed else 'FAIL'}] {claim}")
    return passed


def show_context(label: str, ctx) -> None:
    """Print what a run was actually built from — push and pull, separately."""
    print(f"\n  {label}")
    print(f"    pushed : {ctx.sources['pushed']}")
    pulled = {p['source']: p['count'] for p in ctx.sources['pulled']}
    print(f"    pulled : {pulled}")
    if ctx.data_blocks:
        for block in ctx.data_blocks:
            first = block.strip().splitlines()
            print(f"      {first[0]}")
            for line in first[1:]:
                print(f"      {line}")
    else:
        print("      (no quoted data)")
