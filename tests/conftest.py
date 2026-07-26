"""tests.conftest — keep every test off the real stores.

Owner: Berat Furkan Kocak (HW2).

HW1 had one store and every test remembered to redirect it. HW2 has three
(`knowledge_graph.json`, `radf.db`, `memory.json`), and "remembered to redirect
it" does not scale — a single test that forgets writes a decision into the
developer's real overlay, and nothing fails to tell you.

So redirection is autouse and applies to every test in the suite. A test that
wants its own path still overrides it with monkeypatch.setenv as before; this
only guarantees the default is never the real thing.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_stores(tmp_path_factory, monkeypatch):
    """Point all three stores at a per-test temp directory."""
    box = tmp_path_factory.mktemp("stores")
    monkeypatch.setenv("RADF_GRAPH_PATH", str(box / "knowledge_graph.json"))
    monkeypatch.setenv("RADF_DB_PATH", str(box / "radf.db"))
    monkeypatch.setenv("RADF_MEMORY_PATH", str(box / "memory.json"))
    monkeypatch.setenv("RADF_RUNS_DIR", str(box / "runs"))
    return box
