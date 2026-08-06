"""tracing.setup — turn MLflow tracing on, once, for a process (T15.2).

Owner: Berat Furkan Kocak.

Three decisions live here, all of them load-bearing:

**The backend is sqlite, not the file store.** MLflow 3 puts `./mlruns` in
maintenance mode and *raises* on it unless `MLFLOW_ALLOW_FILE_STORE=true`, so the
obvious default is a dead end. Traces go to `store/mlflow.db`, which the existing
`store/*.db` gitignore rule already covers. `store/` remains off limits to tools
(CLAUDE.md §7.2) — the tracing db does not change that, and no tool reads it.

**Tracing is opt-in.** Nothing is enabled at import. `init_tracing()` is called by
an entry point that wants traces (the eval runners, `main.py --trace`, the
service), never by a library module. HW1-HW5 keep running with `mlflow`
uninstalled, which is a graded gate (decision #90).

**A tracing failure never fails a run.** Every helper here returns a bool or None
and swallows its own errors. An observability layer that can take the agent down
with it is worse than no observability layer.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "store" / "mlflow.db"

DEFAULT_EXPERIMENT = "radf"

_ENABLED = False
_LOCK = threading.Lock()


def tracking_uri() -> str:
    """Where traces are written. `RADF_MLFLOW_URI` is read at call time.

    Same rule as `RADF_GRAPH_PATH` (decision #11) and `RADF_RUNS_DIR`: read at
    call time so a test can redirect the store without re-importing anything.
    """
    override = os.environ.get("RADF_MLFLOW_URI")
    if override:
        return override
    _DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{_DEFAULT_DB}"


def init_tracing(
    experiment: Optional[str] = None,
    *,
    uri: Optional[str] = None,
    autolog: bool = True,
) -> bool:
    """Enable tracing for this process. Idempotent. Returns whether it is on.

    `autolog` turns on `mlflow.langchain.autolog()`, which covers the LangGraph
    path's LLM calls. It does NOT cover tool executions — `agentlib/graph.py`'s
    tools node is hand-written, not LangChain's `ToolNode`, so there are no
    `TOOL` spans unless we make them (decision #88). The raw Zen path
    (`agentlib/core.call`) is likewise hand-instrumented.

    Returns False rather than raising when `mlflow` is missing: the caller gets
    an untraced run, not a crash.
    """
    global _ENABLED
    with _LOCK:
        if _ENABLED:
            return True
        try:
            import mlflow
        except ImportError:
            return False
        try:
            mlflow.set_tracking_uri(uri or tracking_uri())
            mlflow.set_experiment(experiment or os.environ.get("RADF_MLFLOW_EXPERIMENT")
                                  or DEFAULT_EXPERIMENT)
            if autolog:
                try:
                    import mlflow.langchain

                    mlflow.langchain.autolog()
                except Exception:  # noqa: BLE001
                    # Autolog is an enrichment. Losing it costs LLM-span detail on
                    # the LangGraph path; losing the root and tool spans would cost
                    # the whole homework, and those are ours.
                    pass
        except Exception:  # noqa: BLE001
            return False
        _ENABLED = True
        return True


def tracing_enabled() -> bool:
    """True iff `init_tracing()` has succeeded in this process."""
    return _ENABLED


def disable_tracing() -> None:
    """Turn tracing back off. For tests and for the demos that must run clean."""
    global _ENABLED
    with _LOCK:
        _ENABLED = False


def flush() -> None:
    """Wait for pending trace writes. **Call this before reading a trace back.**

    MLflow logs traces asynchronously. Without this, `get_trace(trace_id)`
    immediately after a run returns `None` with a "span data is corrupted"
    warning — which reads like a broken trace and is a race. Every
    read-after-write path in the eval harness and the safety batch calls it.
    """
    if not _ENABLED:
        return
    try:
        import mlflow

        mlflow.flush_trace_async_logging()
    except Exception:  # noqa: BLE001
        pass
