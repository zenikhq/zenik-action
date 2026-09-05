"""Full index pass over a repository.

Ties the pieces together: walk source files -> extract symbols/chunks/imports
(Layer A + definition/import Layer B) -> harvest references (call/reference Layer B)
-> resolve everything into edges -> embed chunks. Produces an `IndexResult` that
maps 1:1 onto schema.sql's symbols/edges/chunks tables.

Runs standalone against any checkout, with or without a network (see embeddings).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from . import languages
from .edges import build_edges
from .embeddings import Embedder, get_embedder
from .extract import RawImport, extract_file
from .model import Chunk, Edge, IndexResult, Symbol
from .references import RawRef, harvest_references


def _git_head_sha(repo_path: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path,
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def _rel_posix(abs_path: str, root: str) -> str:
    return os.path.relpath(abs_path, root).replace(os.sep, "/")


_GO_MODULE_RX = re.compile(r"^\s*module\s+(?P<path>\S+)", re.MULTILINE)


def read_go_module(repo_path: str) -> Optional[str]:
    """Module path from `<root>/go.mod` (`module github.com/x/repo`), or None
    when the repo has no root go.mod. Only the root file is consulted: a
    multi-module monorepo without a root go.mod keeps suffix-only scoping."""
    try:
        text = (Path(repo_path) / "go.mod").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _GO_MODULE_RX.search(text)
    return m.group("path").strip('"') if m else None


def build_index(
    repo_path: str,
    *,
    embedder: Optional[Embedder] = None,
    embed: bool = True,
    commit_sha: Optional[str] = None,
    progress: bool = True,
) -> IndexResult:
    """Index a repository directory into symbols/edges/chunks."""
    root = str(Path(repo_path).resolve())
    sha = commit_sha or _git_head_sha(root)

    all_symbols: list[Symbol] = []
    all_chunks: list[Chunk] = []
    file_imports: list[tuple[str, list[RawImport]]] = []
    all_refs: list[RawRef] = []

    n_files = 0
    for abs_path, lang in languages.iter_source_files(root):
        rel = _rel_posix(abs_path, root)
        try:
            with open(abs_path, "rb") as f:
                source_bytes = f.read()
        except OSError:
            continue

        fx = extract_file(rel, source_bytes, lang, commit_sha=sha)
        if fx is None:
            continue

        n_files += 1
        all_symbols.append(fx.module_symbol)
        all_symbols.extend(fx.symbols)
        all_chunks.extend(fx.chunks)
        file_imports.append((fx.module_symbol.key(), fx.imports))

        refs = harvest_references(
            source_bytes, lang, fx.symbols, fx.module_symbol.key()
        )
        all_refs.extend(refs)

        if progress and n_files % 200 == 0:
            print(f"[indexer] parsed {n_files} files "
                  f"({len(all_symbols)} symbols so far)...")

    if progress:
        print(f"[indexer] parsed {n_files} files -> "
              f"{len(all_symbols)} symbols, {len(all_chunks)} chunks; resolving edges...")

    edges: list[Edge] = build_edges(all_symbols, file_imports, all_refs,
                                    go_module=read_go_module(root))

    if embed and all_chunks:
        emb = embedder or get_embedder()
        if progress:
            print(f"[indexer] embedding {len(all_chunks)} chunks via {emb.name}...")
        vectors = emb.embed([c.text or "" for c in all_chunks])
        for chunk, vec in zip(all_chunks, vectors):
            chunk.embedding = vec

    result = IndexResult(
        repo_path=root, commit_sha=sha,
        symbols=all_symbols, edges=edges, chunks=all_chunks,
    )
    if progress:
        print(f"[indexer] done: {result.stats()}")
    return result
