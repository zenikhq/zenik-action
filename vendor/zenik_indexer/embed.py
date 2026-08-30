"""Embeddings via the OpenAI REST API over urllib.

No SDK dependency on purpose: this package runs inside the *client's* CI runner,
and the prior prototype's rule was to keep what we install there minimal.

Vectors are cached on disk keyed by content hash, so re-indexing an unchanged
repo costs nothing. That cache is also what makes incremental indexing a small
change later rather than a rewrite (BUILD.md §9.3).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL = "text-embedding-3-small"
DIM = 1536  # matches chunks.embedding in schema.sql — do not change one without the other
BATCH = 96
MAX_RETRIES = 5
ENDPOINT = "https://api.openai.com/v1/embeddings"


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    def __init__(self, api_key: str | None = None, cache_dir: str | Path | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        if not self.api_key:
            raise EmbeddingError(
                "OPENAI_API_KEY is not set (add it to the workspace .env.zenik)"
            )
        self.cache_path = Path(cache_dir or ".zenik-cache") / "embeddings.json"
        self._cache: dict[str, list[float]] = {}
        if self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    def _post(self, inputs: list[str]) -> list[list[float]]:
        body = json.dumps({"model": MODEL, "input": inputs}).encode("utf-8")
        req = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        delay = 1.0
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    payload = json.loads(resp.read())
                ordered = sorted(payload["data"], key=lambda d: d["index"])
                return [d["embedding"] for d in ordered]
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                detail = exc.read().decode("utf-8", "replace")[:400]
                raise EmbeddingError(f"OpenAI {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
        raise EmbeddingError(f"embedding request failed: {last}")

    def embed(self, texts: list[str], hashes: list[str] | None = None,
              progress=None) -> list[list[float]]:
        import hashlib

        hashes = hashes or [hashlib.sha256(t.encode()).hexdigest() for t in texts]
        out: list[list[float] | None] = [self._cache.get(h) for h in hashes]

        todo = [i for i, v in enumerate(out) if v is None]
        for start in range(0, len(todo), BATCH):
            idxs = todo[start : start + BATCH]
            vectors = self._post([texts[i][:32000] for i in idxs])
            for i, vec in zip(idxs, vectors):
                if len(vec) != DIM:
                    raise EmbeddingError(f"model returned dim {len(vec)}, expected {DIM}")
                out[i] = vec
                self._cache[hashes[i]] = vec
            if progress:
                progress(min(start + BATCH, len(todo)), len(todo))

        self.flush()
        return [v for v in out if v is not None]

    def flush(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache))
        tmp.replace(self.cache_path)
