"""retrieval.embed — text -> vectors, via OpenRouter.

Owner: Berat Furkan Kocak (HW5, T14.6d).

`text-embedding-3-small`, 1536 dimensions, over plain `urllib.request`. No
embedding SDK: the §4 HW5 amendment lifted the ban for *an endpoint*, not for a
client library, and this is one POST with a JSON body — the same reasoning that
keeps `channel/telegram.py` on `urllib` rather than a Telegram package.

Cost, measured rather than assumed: ~$0.02 per 1M tokens, and this repo's whole
corpus is ~60k tokens, so a full re-index is about **$0.001**. That number is
why there is no local model here — on a 7 GB machine already running Postgres, a
torch install would cost more in RAM than the API costs in dollars.

**The cache is not an optimization — it is what makes the numbers reproducible.**
Keyed on `sha256(model + text)`, so re-indexing an unchanged chunk is free and
the Part 2 metrics can be re-run as many times as the k-sweep needs without a
new bill.

The reproducibility half is the part worth stating, because it is **measured and
it is not what you would assume**: `text-embedding-3-small` is *not*
bit-deterministic. Embedding the same string twice returns vectors differing by
up to ~1.2e-4 per component (`tests/test_retrieval_online.py` asserts the bound).
Cosine similarity between the two is ~1.0, so rankings are stable in general —
but two chunks separated by less than that margin can swap, and nDCG reads
position. So identical eval runs are guaranteed identical **because of the
cache**, not because the endpoint is deterministic. Re-running with
`--no-cache` may legitimately move a metric in the fourth decimal place.

Storage lives in `retrieval.cache` — one sqlite file, vectors packed as float32.
It is derived data and is gitignored; deleting it costs $0.002 to rebuild.

The API key is `OPENROUTER_API_KEY`, deliberately separate from
`OPENCODE_API_KEY`: different provider, different account, different spend.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Iterable, Optional, Sequence

from .cache import cache_dir, count_embeddings, embed_key, get_embeddings, put_embeddings

EMBED_URL = "https://openrouter.ai/api/v1/embeddings"
DEFAULT_MODEL = "openai/text-embedding-3-small"
EMBED_DIM = 1536

# Batch size, not a tuning knob: the endpoint accepts a list, and 64 keeps a
# single request comfortably under any body limit while cutting a ~700-chunk
# index from 700 round trips to 11.
BATCH = 64

class EmbeddingError(RuntimeError):
    """The embedding endpoint could not be reached or returned nonsense.

    Its own exception so `search_corpus` can branch to `index_unavailable`
    rather than letting an empty vector flow onward as if it were a real one
    (Part B, B2 — a tool failure gets its own branch).
    """


def embed_model() -> str:
    return os.environ.get("RADF_EMBED_MODEL") or DEFAULT_MODEL


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key or key.endswith("...") or key in {"sk-or-v1-...", "changeme"}:
        raise EmbeddingError(
            "OPENROUTER_API_KEY is unset or a placeholder — see .env.example"
        )
    return key


def _post(payload: dict, key: str, *, timeout: float, retries: int = 3) -> dict:
    body = json.dumps(payload).encode("utf-8")
    last: Optional[Exception] = None
    for attempt in range(retries):
        request = urllib.request.Request(
            EMBED_URL,
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            # 4xx other than rate-limit will not fix itself by waiting.
            if exc.code != 429 and 400 <= exc.code < 500:
                raise EmbeddingError(f"embeddings HTTP {exc.code}: {detail}") from exc
            last = EmbeddingError(f"embeddings HTTP {exc.code}: {detail}")
        except Exception as exc:  # noqa: BLE001 — timeouts, DNS, reset
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise EmbeddingError(f"embeddings unreachable after {retries} attempts: {last}")


def embed_texts(
    texts: Sequence[str],
    *,
    model: Optional[str] = None,
    use_cache: bool = True,
    timeout: float = 60.0,
) -> list[list[float]]:
    """Embed `texts`, in order. Cached per text.

    Raises `EmbeddingError` rather than returning short or zero vectors: a
    silently-wrong vector produces a plausible-looking ranking that is entirely
    noise, which is the worst failure this module could have.
    """
    name = model or embed_model()
    out: list[Optional[list[float]]] = [None] * len(texts)
    todo: list[int] = []

    keys = [embed_key(name, t) for t in texts]
    if use_cache:
        # One query for the whole batch rather than one stat() per text: the
        # indexer asks about ~950 texts at once.
        cached = get_embeddings(keys)
        for i, key in enumerate(keys):
            hit = cached.get(key)
            if hit is None:
                todo.append(i)
            else:
                out[i] = hit
    else:
        todo = list(range(len(texts)))

    if todo:
        key = _api_key()
        for start in range(0, len(todo), BATCH):
            idxs = todo[start:start + BATCH]
            payload = {"model": name, "input": [texts[i] for i in idxs]}
            data = _post(payload, key, timeout=timeout)
            items = data.get("data") or []
            if len(items) != len(idxs):
                raise EmbeddingError(
                    f"embeddings returned {len(items)} vectors for {len(idxs)} inputs"
                )
            # The API is documented to preserve order, but it also returns an
            # explicit `index` — trusting the field over the position costs
            # nothing and cannot silently mismatch a vector to a chunk.
            for item in items:
                pos = item.get("index", items.index(item))
                vector = item.get("embedding")
                if not isinstance(vector, list) or len(vector) != EMBED_DIM:
                    raise EmbeddingError(
                        f"expected {EMBED_DIM}-dim embedding, got {type(vector).__name__}"
                        f" of length {len(vector) if isinstance(vector, list) else 'n/a'}"
                    )
                out[idxs[pos]] = vector
            if use_cache:
                put_embeddings(name, [(keys[i], out[i]) for i in idxs if out[i] is not None])

    missing = [i for i, v in enumerate(out) if v is None]
    if missing:
        raise EmbeddingError(f"no embedding produced for {len(missing)} input(s)")
    return out  # type: ignore[return-value]


def embed_query(query: str, **kwargs) -> list[float]:
    """One query vector. Same model and cache as the corpus — a query embedded
    with a different model than the index is not comparable to it at all."""
    return embed_texts([query], **kwargs)[0]


def cached_count(texts: Iterable[str], *, model: Optional[str] = None) -> int:
    """How many of `texts` are already cached — for `retrieval.index`'s report."""
    name = model or embed_model()
    return count_embeddings([embed_key(name, t) for t in texts])
