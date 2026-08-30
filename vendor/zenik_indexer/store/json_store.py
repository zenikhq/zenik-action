"""Local JSON persistence for an IndexResult.

Used by the standalone CLI: `index` writes one of these, `impact --index` reads
it back. Deliberately drops `Chunk.text` on write — only the embedding and
metadata survive — so the on-disk index mirrors the server's trust stance (no raw
source at rest) and stays a fraction of the size.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..model import Chunk, Edge, IndexResult, Symbol

_FORMAT = 1


def save_index(result: IndexResult, path: str) -> None:
    payload = {
        "format": _FORMAT,
        "repo_path": result.repo_path,
        "commit_sha": result.commit_sha,
        "symbols": [asdict(s) for s in result.symbols],
        "edges": [asdict(e) for e in result.edges],
        "chunks": [
            {
                "path": c.path,
                "language": c.language,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "content_hash": c.content_hash,
                "symbol": c.symbol,
                "embedding": c.embedding,   # text intentionally omitted
            }
            for c in result.chunks
        ],
    }
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


def load_index(path: str) -> IndexResult:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    symbols = [Symbol(**s) for s in data.get("symbols", [])]
    edges = [Edge(**e) for e in data.get("edges", [])]
    chunks = [
        Chunk(
            path=c["path"], language=c["language"],
            start_line=c["start_line"], end_line=c["end_line"],
            content_hash=c["content_hash"], symbol=c.get("symbol"),
            text=None, embedding=c.get("embedding"),
        )
        for c in data.get("chunks", [])
    ]
    return IndexResult(
        repo_path=data.get("repo_path", ""),
        commit_sha=data.get("commit_sha"),
        symbols=symbols, edges=edges, chunks=chunks,
    )
