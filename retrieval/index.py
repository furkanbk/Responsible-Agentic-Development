"""retrieval.index — build the corpus and load it into pgvector.

Owner: Berat Furkan Kocak (HW5, T14.6h).

    python -m retrieval.index              # full reindex
    python -m retrieval.index --dry-run    # chunk and report, embed nothing
    python -m retrieval.index --kinds doc  # one chunk kind only

The index is **derived**, so a reindex replaces it wholesale — same rule
`scan_repository_structure` follows on the derived graph (#16). A merge would
leave chunks behind for sections that no longer exist, and a confident retrieval
hit on deleted documentation is worse than a miss.

`--dry-run` exists because chunking is the part with judgment in it: it prints
the per-kind counts and size distribution without spending a cent, which is how
you check a chunking change before paying to embed it.
"""

from __future__ import annotations

import argparse
import sys

from overlay.db import connect as overlay_connect

from .chunker import build_corpus
from .embed import EmbeddingError, cached_count, embed_model, embed_texts
from .store import IndexUnavailable, connect, counts_by_kind, ensure_schema, replace_all


def _report(chunks) -> None:
    by_kind: dict[str, int] = {}
    for chunk in chunks:
        by_kind[chunk.kind] = by_kind.get(chunk.kind, 0) + 1
    sizes = sorted(len(c.text) for c in chunks)
    total = len(chunks)
    print(f"corpus: {total} chunks")
    for kind in sorted(by_kind):
        print(f"  {kind:<10} {by_kind[kind]:>5}")
    if sizes:
        median = sizes[len(sizes) // 2]
        print(f"  chars: min={sizes[0]} median={median} max={sizes[-1]}"
              f" total={sum(sizes):,}")
        print(f"  approx tokens: {sum(sizes) // 4:,}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the RADF retrieval index.")
    parser.add_argument("--dry-run", action="store_true",
                        help="chunk and report only; no embeddings, no database")
    parser.add_argument("--kinds", default="component,decision,doc",
                        help="comma-separated subset of component,decision,doc")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore the embedding cache (re-embeds everything, costs money)")
    args = parser.parse_args(argv)

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())

    overlay = overlay_connect()
    try:
        chunks = build_corpus(overlay, kinds=kinds)
    finally:
        overlay.close()

    _report(chunks)
    if not chunks:
        print("nothing to index — has `python -m overlay.summarize` run?", file=sys.stderr)
        return 1

    texts = [c.text for c in chunks]
    hits = cached_count(texts)
    print(f"embeddings: {hits}/{len(texts)} cached, {len(texts) - hits} to fetch"
          f" (model {embed_model()})")

    if args.dry_run:
        print("dry run — nothing embedded, nothing written")
        return 0

    try:
        vectors = embed_texts(texts, use_cache=not args.no_cache)
    except EmbeddingError as exc:
        print(f"embedding failed: {exc}", file=sys.stderr)
        return 2

    try:
        with connect() as conn:
            ensure_schema(conn)
            written = replace_all(conn, chunks, vectors)
            print(f"indexed: {written} rows -> {counts_by_kind(conn)}")
    except IndexUnavailable as exc:
        print(f"index unavailable: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
