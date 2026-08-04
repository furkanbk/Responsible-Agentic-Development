"""Online tests for the HW5 retrieval layer.

Owner: Berat Furkan Kocak (HW5, T14.6i).

**Every test here is online by construction** (CLAUDE.md §8): each one calls the
real OpenRouter embeddings endpoint and writes real vectors into the real
running Postgres. That is deliberate and it is the point — the bugs this layer
is prone to are exactly the ones a mock cannot catch:

  - a 1536-dim contract that silently becomes 3072 when the model id changes,
  - a `vector` cast that works in psql and fails from psycopg,
  - a visibility predicate that is correct in SQL and never actually applied,
  - a distance returned where a similarity was assumed, reversing the ranking
    while every offline assertion still passes.

**The test table is dropped afterwards.** Every test runs inside the `pg_dsn`
fixture from `conftest.py`, which creates a uniquely-named schema, points
`RADF_PG_DSN` at it, and `DROP SCHEMA ... CASCADE`s on teardown. The developer's
real index is never touched, and two of these running concurrently cannot see
each other's rows. `isolate_stores` separately redirects the sqlite overlay and
the embedding cache into a temp dir, so a run here also cannot warm or poison
the real cache.

Skips rather than fails when Postgres is down or the API key is absent — a test
that cannot run is not a test that failed, and conflating them trains people to
ignore red.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _online import have_real_key  # noqa: E402

from overlay.db import connect as overlay_connect, insert_decision, upsert_node_summary  # noqa: E402
from retrieval import chunker, store  # noqa: E402
from retrieval.bm25 import BM25  # noqa: E402
from retrieval.embed import EMBED_DIM, EmbeddingError, embed_query, embed_texts  # noqa: E402
from retrieval.fuse import rrf  # noqa: E402
from retrieval.search import IndexEmpty, search  # noqa: E402
from retrieval.types import Chunk  # noqa: E402

pytestmark = pytest.mark.online


def _require_key(monkeypatch) -> None:
    """Export the real OpenRouter key for this test only, or skip."""
    from dotenv import dotenv_values

    root = Path(__file__).resolve().parent.parent
    values = dotenv_values(root / ".env")
    key = (values.get("OPENROUTER_API_KEY") or "").strip()
    if not key or key.endswith("...") or key == "sk-or-v1-...":
        pytest.skip("OPENROUTER_API_KEY unset or a placeholder — see .env.example")
    monkeypatch.setenv("OPENROUTER_API_KEY", key)


@pytest.fixture
def live(pg_dsn, monkeypatch):
    """A live, empty index in a throwaway schema, with embeddings available."""
    _require_key(monkeypatch)
    with store.connect() as conn:
        store.ensure_schema(conn)
        yield conn


def _index(conn, chunks: list[Chunk]) -> None:
    vectors = embed_texts([c.text for c in chunks])
    store.replace_all(conn, chunks, vectors)


def _chunk(chunk_id: str, text: str, **kw) -> Chunk:
    kw.setdefault("kind", "doc")
    kw.setdefault("heading_path", chunk_id)
    return Chunk(chunk_id=chunk_id, text=text, **kw)


# --- 1. the embedding contract ------------------------------------------------

def test_embeddings_are_1536_dim_and_near_but_not_bit_deterministic(pg_dsn, monkeypatch):
    """Dimension is a schema constant; a model swap that changes it must fail
    here rather than at `INSERT`, where the error names a cast, not the cause.

    The second half pins a measured surprise: `text-embedding-3-small` is **not**
    bit-deterministic. The same string embedded twice differs by ~1e-4 per
    component. Cosine similarity stays ~1.0 so rankings are stable in general,
    but two chunks closer together than that margin can swap — and nDCG reads
    position. This is why the eval harness's reproducibility rests on the
    embedding cache rather than on the endpoint, and the bound is asserted so a
    provider change that made it worse would be caught here.
    """
    _require_key(monkeypatch)
    first = embed_query("the approval gate blocks destructive tools")
    assert len(first) == EMBED_DIM
    assert all(isinstance(v, float) for v in first[:8])

    second = embed_query("the approval gate blocks destructive tools", use_cache=False)
    assert len(second) == EMBED_DIM

    drift = max(abs(a - b) for a, b in zip(first, second))
    assert drift < 1e-3, f"embedding drift {drift} is larger than documented"

    dot = sum(a * b for a, b in zip(first, second))
    norm = (sum(a * a for a in first) ** 0.5) * (sum(b * b for b in second) ** 0.5)
    assert dot / norm > 0.9999, "same text should embed to a near-identical direction"


# --- 2. the cache is real ------------------------------------------------------

def test_embedding_cache_avoids_a_second_call(pg_dsn, monkeypatch, tmp_path):
    """A cache miss then a hit, proven by breaking the key in between.

    If the second call still succeeds with a placeholder key, the value came
    from disk — which is the property the eval harness's free re-runs rest on.
    """
    _require_key(monkeypatch)
    monkeypatch.setenv("RADF_RETRIEVAL_CACHE", str(tmp_path / "cache"))

    text = "reciprocal rank fusion combines two rankings by position"
    warm = embed_texts([text])[0]

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-...")
    cached = embed_texts([text])[0]
    assert cached == warm

    with pytest.raises(EmbeddingError):
        embed_texts(["a text that was never embedded before, so it must call out"])


# --- 3. chunk -> embed -> Postgres round trip ---------------------------------

def test_chunks_reach_postgres_with_vectors(live):
    """The end-to-end write path, including the `::vector` cast."""
    chunks = [
        _chunk("c_alpha", "The approval gate requires an explicit y before a destructive tool runs."),
        _chunk("c_beta", "Reciprocal rank fusion merges the dense and lexical rankings."),
        _chunk("c_gamma", "Node summaries live in the sqlite overlay, never in the derived graph."),
    ]
    _index(live, chunks)

    assert store.count(live) == 3
    with live.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {store.TABLE} WHERE embedding IS NOT NULL")
        assert cur.fetchone()[0] == 3
        cur.execute(f"SELECT vector_dims(embedding) FROM {store.TABLE} LIMIT 1")
        assert cur.fetchone()[0] == EMBED_DIM


# --- 4. dense search actually ranks by meaning --------------------------------

def test_dense_search_ranks_by_meaning_not_words(live):
    """A query sharing NO content words with the right chunk must still find it.

    This is the one property BM25 cannot have, so if it fails the dense arm is
    decorative.
    """
    chunks = [
        _chunk("c_stall", "Repeating the identical tool call twice ends the run instead of continuing."),
        _chunk("c_pasta", "Boil the water, salt it well, and cook the pasta until al dente."),
        _chunk("c_ports", "The container publishes port 5433 so it cannot collide with a local server."),
    ]
    _index(live, chunks)

    hits = store.dense_search(live, embed_query("how does it avoid an infinite loop"), n=3)
    assert hits, "dense search returned nothing"
    assert hits[0][0].chunk_id == "c_stall"
    # `<=>` is a DISTANCE; store.dense_search must return a SIMILARITY.
    assert 0.0 <= hits[0][1] <= 1.0
    assert hits[0][1] > hits[-1][1]


# --- 5. the lexical arm rescues exact identifiers -----------------------------

def test_bm25_beats_dense_on_a_rare_identifier(live):
    """The reason BM25 is here: IDF on a rare token.

    Embeddings tokenize `impact_scope` into pieces and match it against prose
    about scope generally; BM25 matches the literal term and IDF makes it heavy.
    """
    chunks = [
        _chunk("c_impact", "impact_scope bounds which symbol_uids a run may write to."),
        _chunk("c_scope1", "Scope in this project is ambient rather than passed as an argument."),
        _chunk("c_scope2", "The scope of a change is decided before the executor runs."),
        _chunk("c_scope3", "Writing outside the permitted scope is refused by the tool itself."),
    ]
    _index(live, chunks)

    bm25 = BM25([(c.chunk_id, c.text) for c in store.all_chunks(live)])
    assert bm25.top("impact_scope", 1)[0][0] == "c_impact"

    hits = search("impact_scope", k=1, rerank=False, conn=live)
    assert hits[0].chunk.chunk_id == "c_impact"


# --- 6. fusion recovers what one arm alone misses -----------------------------

def test_rrf_promotes_the_chunk_both_arms_agree_on(live):
    """RRF's actual job: agreement beats a single arm's confident top hit."""
    chunks = [
        _chunk("c_both", "The stall detector stops a run when a tool call repeats identically."),
        _chunk("c_lex", "stall stall stall repeated tokens without any explanation of behaviour."),
        _chunk("c_sem", "Runs are terminated when no progress is being made by the model."),
    ]
    _index(live, chunks)

    dense = [c.chunk_id for c, _ in store.dense_search(live, embed_query("stall detection"), n=3)]
    bm25 = BM25([(c.chunk_id, c.text) for c in store.all_chunks(live)])
    lexical = [cid for cid, _ in bm25.top("stall detection", 3)]

    fused = rrf([dense, lexical])
    assert fused[0][0] == "c_both", f"dense={dense} lexical={lexical} fused={fused}"


# --- 7. visibility is enforced in the query -----------------------------------

def test_private_chunks_are_filtered_in_sql_not_in_the_prompt(live, monkeypatch):
    """Decision #24, on the retrieval path.

    Both arms must apply the predicate — a lexical arm reading rows the dense
    arm cannot would leak the row through the fused ranking.
    """
    chunks = [
        Chunk(chunk_id="c_team", kind="decision", text="Team decision: prune is the only gated tool.",
              visibility="team"),
        Chunk(chunk_id="c_mine", kind="decision", text="Private note from berat about gated tools.",
              visibility="user:berat"),
        Chunk(chunk_id="c_theirs", kind="decision", text="Private note from dias about gated tools.",
              visibility="user:dias"),
    ]
    _index(live, chunks)

    from agentlib.session import session_scope

    with session_scope("berat"):
        visible = {c.chunk_id for c in store.all_chunks(live)}
        assert visible == {"c_team", "c_mine"}
        dense_ids = {c.chunk_id for c, _ in store.dense_search(live, embed_query("gated tools"), n=5)}
        assert "c_theirs" not in dense_ids
        assert {h.chunk.chunk_id for h in search("gated tools", k=5, rerank=False, conn=live)} <= visible

    # No session at all sees team rows only.
    assert {c.chunk_id for c in store.all_chunks(live)} == {"c_team"}


# --- 8. the real chunker over the real overlay --------------------------------

def test_real_corpus_chunks_and_indexes(live, tmp_path):
    """The production chunker, not fixtures: a summary card and a decision row
    both make it into Postgres with the right kind and a joined `symbol_uid`."""
    overlay = overlay_connect()
    try:
        upsert_node_summary(
            overlay, component="agentlib/guards.py", symbol="detect_stall",
            summary="Returns True when the last two call signatures are identical.",
            responsibility="Stops a run that is repeating itself.",
            signature="detect_stall(signatures) -> bool",
            content_sha="sha-1", author_id="test",
        )
        insert_decision(
            overlay, component="agentlib/guards.py",
            decision="Stall detection is code-owned, not model-decided.",
            rationale="A model asked to notice its own repetition does not.",
            status="accepted", author_id="test",
        )
        chunks = chunker.build_corpus(overlay, docs=())
    finally:
        overlay.close()

    kinds = {c.kind for c in chunks}
    assert kinds == {"component", "decision"}
    _index(live, chunks)

    by_kind = store.counts_by_kind(live)
    assert by_kind.get("component", 0) >= 1 and by_kind.get("decision", 0) >= 1

    hits = search("what stops a run that repeats itself", k=3, rerank=False, conn=live)
    top = hits[0].chunk
    assert top.symbol_uid == "Module:agentlib.guards"


# --- 9. markdown chunking keeps table rows atomic -----------------------------

def test_table_rows_survive_chunking_and_are_retrievable(live, tmp_path):
    """The chunking decision that motivated the whole strategy.

    A decision-log row must arrive as ONE chunk carrying its heading path, or
    the decision is severed from its rationale and neither half answers.
    """
    doc = tmp_path / "docs"
    doc.mkdir()
    (doc / "LOG.md").write_text(
        "# Log\n\n## 5. Decision log\n\n"
        "| # | Decision | Rejected | Why |\n|---|---|---|---|\n"
        "| 21 | The structural layer stays JSON and disposable | migrating nodes into SQLite |"
        " building a schema for data we decided not to own is throwaway work |\n"
        "| 22 | symbol_uid becomes real across the overlay | joining on a bare component string |"
        " the GitNexus swap is only a uid remap if nothing stores raw strings |\n",
        encoding="utf-8",
    )

    chunks = chunker.doc_chunks(["docs/LOG.md"], root=tmp_path)
    assert len(chunks) == 2, [c.text for c in chunks]
    for chunk in chunks:
        assert chunk.heading_path.endswith("5. Decision log")
        assert "Decision | Rejected | Why" in chunk.text     # header retained
        assert chunk.text.count("\n|") >= 1

    _index(live, chunks)
    hits = search("why is the structural layer still a JSON file", k=2, rerank=False, conn=live)
    assert "throwaway work" in hits[0].chunk.text
    assert "disposable" in hits[0].chunk.text  # decision and rationale in ONE chunk


# --- 10. the tool contract, end to end ----------------------------------------

def test_search_corpus_tool_returns_retriever_order_and_real_errors(live):
    """`search_corpus`'s contract over a live index, including its error branch."""
    from tools.retrieval_tools import search_corpus

    chunks = [
        _chunk("c_gate", "The approval gate requires an explicit y before a destructive tool runs.",
               kind="doc", symbol_uid="Doc:docs/ARCHITECTURE.md"),
        _chunk("c_rrf", "Reciprocal rank fusion merges the two rankings by position.", kind="doc"),
        _chunk("c_pg", "Postgres holds the derived retrieval index and may be rebuilt.", kind="doc"),
    ]
    _index(live, chunks)

    out = search_corpus("how are destructive tools approved", k=2, rerank=False)
    assert out["count"] == 2 and out["reranked"] is False
    assert [r["rank"] for r in out["results"]] == [1, 2]
    assert out["results"][0]["chunk_id"] == "c_gate"
    assert set(out["results"][0]) == {
        "chunk_id", "kind", "symbol_uid", "symbol", "heading_path",
        "source_path", "rank", "score", "text",
    }

    # `source` narrows by kind, in the query.
    assert search_corpus("anything at all", k=5, rerank=False, source="components")["count"] == 0

    # An empty index is its own branch, never an empty result set.
    store.replace_all(live, [], [])
    assert search_corpus("anything at all", k=3, rerank=False) == {"error": "index_empty"}
