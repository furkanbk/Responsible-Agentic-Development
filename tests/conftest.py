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

import os
import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_stores(tmp_path_factory, monkeypatch):
    """Point every store at a per-test temp directory."""
    box = tmp_path_factory.mktemp("stores")
    monkeypatch.setenv("RADF_GRAPH_PATH", str(box / "knowledge_graph.json"))
    monkeypatch.setenv("RADF_DB_PATH", str(box / "radf.db"))
    monkeypatch.setenv("RADF_MEMORY_PATH", str(box / "memory.json"))
    monkeypatch.setenv("RADF_RUNS_DIR", str(box / "runs"))
    # HW5. The cache is redirected but the DSN is NOT: Postgres is a shared
    # service, not a file, so there is no per-test copy of it to point at. Tests
    # that need it take the `pg_dsn` fixture below, which isolates by SCHEMA
    # rather than by path.
    monkeypatch.setenv("RADF_RETRIEVAL_CACHE", str(box / "cache"))
    return box


# --- HW5: Postgres ------------------------------------------------------------

DEFAULT_PG_DSN = "postgresql://radf:radf@localhost:5433/radf"


def configured_dsn() -> str:
    """The DSN to test against: env, then .env, then the compose default.

    Reads `.env` with `dotenv_values` rather than `load_dotenv` so that merely
    running the suite never exports anything into the process — the same rule
    `tests/_online.py` follows for the API key.
    """
    dsn = os.environ.get("RADF_PG_DSN")
    if dsn:
        return dsn
    try:
        from dotenv import dotenv_values
    except ImportError:
        return DEFAULT_PG_DSN
    root = Path(__file__).resolve().parent.parent
    return dotenv_values(root / ".env").get("RADF_PG_DSN") or DEFAULT_PG_DSN


@pytest.fixture
def pg_dsn(monkeypatch):
    """A live Postgres in a schema of this test's own, or skip.

    Skips — rather than fails — when the container is down or `psycopg` is not
    installed, so the offline suite keeps passing on a machine with no Docker.
    That is the same trade `@pytest.mark.online` makes: a test that cannot run
    is not the same as a test that failed, and conflating them trains people to
    ignore red.

    Isolation is by SCHEMA, not by database: `isolate_stores` gives every other
    store a private temp path, but Postgres is one shared service, so each test
    gets a uniquely-named schema dropped on teardown. Two tests running against
    the same container therefore cannot see each other's chunks.
    """
    dsn = configured_dsn()
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not installed — pip install -r requirements.txt")

    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — any failure to reach it is a skip
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}. Try `docker compose up -d`.")

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f'CREATE SCHEMA "{schema}"')
    conn.close()

    # `public` stays on the path, second. The `vector` extension installs its
    # type there, and a search_path of the test schema alone cannot resolve
    # `vector(1536)` at all. Test schema first, so tables are still created in
    # it and never in public.
    scoped = f"{dsn}?options=-csearch_path%3D{schema},public"
    monkeypatch.setenv("RADF_PG_DSN", scoped)
    yield scoped

    cleanup = psycopg.connect(dsn, connect_timeout=3)
    with cleanup:
        with cleanup.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    cleanup.close()
