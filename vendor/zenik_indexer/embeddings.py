"""Chunk embeddings — Layer A vectors.

Pluggable by design. `OpenAIEmbedder` (text-embedding-3-small, 1536 dims — exactly
what schema.sql's `chunks.embedding vector(1536)` declares) is the real path. When
no OPENAI_API_KEY is present, `HashEmbedder` produces deterministic pseudo-vectors
of the same dimension so `index` and `impact` still run offline and in tests. The
hash vectors are NOT semantically meaningful — they exist so the pipeline shape is
identical with or without a key; the semantic half of blast radius is simply off.
"""
from __future__ import annotations

import hashlib
import math
import os
from typing import Optional, Protocol

EMBED_DIM = 1536
OPENAI_MODEL = "text-embedding-3-small"
# Keep request bodies well under OpenAI's limits; also bounds memory on big repos.
_BATCH = 128
# Truncate very long chunks before embedding (token cost + model input limit).
# ~8k chars is comfortably within the model's context for a single chunk.
_MAX_CHARS = 8000


class Embedder(Protocol):
    dim: int
    name: str
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic, offline. Hashes token trigrams into a fixed-dim vector and
    L2-normalizes. Same text -> same vector, so cosine similarity is stable, but
    it captures no real semantics. Used only when no embedding key is configured."""
    dim = EMBED_DIM
    name = "hash-fallback"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = text.split()
        for i in range(len(tokens)):
            gram = " ".join(tokens[i:i + 3])
            h = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OpenAIEmbedder:
    """Real embeddings via OpenAI. Batches requests and truncates oversized
    chunks. Raises on construction if the SDK or key is missing so the factory
    can fall back explicitly rather than failing mid-index."""
    dim = EMBED_DIM
    name = OPENAI_MODEL

    def __init__(self, api_key: str):
        from openai import OpenAI  # imported lazily so the dep is optional
        self._client = OpenAI(api_key=api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), _BATCH):
            batch = [t[:_MAX_CHARS] if t else " " for t in texts[i:i + _BATCH]]
            resp = self._client.embeddings.create(model=OPENAI_MODEL, input=batch)
            out.extend(d.embedding for d in resp.data)
        return out


def get_embedder(prefer_offline: bool = False, api_key: Optional[str] = None) -> Embedder:
    """Select an embedder. Uses OpenAI when a key is available and offline isn't
    forced; otherwise the hash fallback. Never raises — a failed OpenAI init
    degrades to offline with a logged note."""
    if prefer_offline:
        return HashEmbedder()
    key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        try:
            return OpenAIEmbedder(key)
        except Exception as e:  # noqa: BLE001
            print(f"[embeddings] OpenAI init failed ({type(e).__name__}); "
                  f"using offline hash embedder")
    else:
        print("[embeddings] no OPENAI_API_KEY; using offline hash embedder "
              "(semantic blast-radius disabled)")
    return HashEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
