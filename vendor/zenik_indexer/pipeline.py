"""Full index build: walk a repo, extract, chunk, embed, push to the platform."""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from . import graph
from .chunker import Chunk, chunk_file
from .client import PlatformClient
from .embed import Embedder
from .extract import FileExtract, extract_file
from .langs import iter_source_files

BATCH_ROWS = 400


@dataclass
class IndexResult:
    repo_id: str | None
    files: int
    failed_files: int
    symbols: int
    edges: int
    chunks: int
    embedded: int
    stats: dict


def scan(root: Path, log=print) -> tuple[list[FileExtract], list[str]]:
    extracts: list[FileExtract] = []
    failures: list[str] = []
    for abs_path in iter_source_files(root):
        rel = str(abs_path.relative_to(root))
        fx = extract_file(abs_path, rel)
        if fx is None:
            continue
        if fx.error:
            failures.append(f"{rel}: {fx.error}")
            continue
        extracts.append(fx)
    log(f"  parsed {len(extracts)} files ({len(failures)} unparseable)")
    return extracts, failures


def build_index(root: Path, log=print):
    extracts, failures = scan(root, log)
    symbols, edges = graph.build(extracts)
    chunks: list[Chunk] = []
    for fx in extracts:
        chunks.extend(chunk_file(fx))
    log(f"  {len(symbols)} symbols, {len(edges)} edges, {len(chunks)} chunks")
    return extracts, symbols, edges, chunks, failures


def head_sha(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unversioned"


def push(
    root: Path,
    repo_full_name: str,
    client: PlatformClient,
    embedder: Embedder | None,
    commit_sha: str | None = None,
    log=print,
) -> IndexResult:
    commit_sha = commit_sha or head_sha(root)
    extracts, symbols, edges, chunks, failures = build_index(root, log)

    repo = client.register_repo(repo_full_name)
    repo_id = repo["repo_id"]
    log(f"  repo {repo_full_name} -> {repo_id}")

    embedded = 0
    vectors: list[list[float]] = []
    if embedder and chunks:
        log(f"  embedding {len(chunks)} chunks...")
        vectors = embedder.embed(
            [c.text for c in chunks],
            [c.content_hash for c in chunks],
            progress=lambda done, total: log(f"    {done}/{total}"),
        )
        embedded = len(vectors)

    # Symbols first: edges and chunks are resolved against them server-side.
    for i in range(0, len(symbols), BATCH_ROWS):
        client.send_batch(repo_id, {
            "commit_sha": commit_sha,
            "symbols": [
                {"kind": s.kind, "name": s.name, "path": s.path,
                 "start_line": s.start_line, "end_line": s.end_line,
                 "language": s.language, "commit_sha": commit_sha}
                for s in symbols[i : i + BATCH_ROWS]
            ],
        })
    log(f"  sent {len(symbols)} symbols")

    for i in range(0, len(edges), BATCH_ROWS):
        client.send_batch(repo_id, {
            "commit_sha": commit_sha,
            "edges": [asdict(e) for e in edges[i : i + BATCH_ROWS]],
        })
    log(f"  sent {len(edges)} edges")

    if vectors:
        for i in range(0, len(chunks), BATCH_ROWS):
            window = chunks[i : i + BATCH_ROWS]
            client.send_batch(repo_id, {
                "commit_sha": commit_sha,
                "chunks": [
                    {"path": c.path, "start_line": c.start_line, "end_line": c.end_line,
                     "language": c.language, "symbol_name": c.symbol_name,
                     "content_hash": c.content_hash, "embedding": v}
                    for c, v in zip(window, vectors[i : i + BATCH_ROWS])
                ],
            })
        log(f"  sent {len(chunks)} chunks")

    client.commit_index(repo_id, commit_sha, replace=True)
    log(f"  index committed at {commit_sha[:12]}")

    return IndexResult(
        repo_id=repo_id, files=len(extracts), failed_files=len(failures),
        symbols=len(symbols), edges=len(edges), chunks=len(chunks),
        embedded=embedded, stats=graph.stats(symbols, edges),
    )
