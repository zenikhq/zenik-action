"""zenik-indexer CLI.

    zenik-indexer stats  <path>                      # parse only. no network, no keys.
    zenik-indexer impact <path> --changed name@file  # blast radius, in memory
    zenik-indexer changed <path> --base main         # git diff -> changed symbols
    zenik-indexer index  <path> --repo owner/name    # build and push to the platform

`stats` and `impact` run with **no Supabase and no API key** (add --semantic to
`impact` to also use embeddings). That is deliberate: BUILD.md §3 wants accuracy
judged against a real repo as a black box before any of the plumbing exists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import graph
from .chunker import chunk_file
from .client import PlatformClient, PlatformError
from .diff import changed_symbols
from .embed import Embedder, EmbeddingError
from .extract import Symbol
from .pipeline import build_index, head_sha, push
from . import standalone


def _load_workspace_env() -> None:
    """Read ../.env.zenik if present. No python-dotenv dependency."""
    for candidate in (
        Path(__file__).resolve().parents[2] / ".env.zenik",
        Path.cwd() / ".env.zenik",
    ):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
        return


def _fmt(hits, tests, stats) -> None:
    if not hits:
        print("  (nothing)")
    for h in hits:
        flag = "  CROSS-LANG" if h.cross_language else ""
        where = f"depth {h.depth}" if h.depth is not None else "semantic only"
        sim = f", sim {h.similarity:.2f}" if h.similarity is not None else ""
        via = "  via " + ">".join(h.via) if h.via else ""
        print(f"  {h.path}:{h.start_line}  {h.name}  [{h.kind}]")
        print(f"      {h.score:.3f}  [{'+'.join(h.sources)}] {where}{sim}{via}{flag}")
    if tests:
        print("\n  Tests likely relevant:")
        for t in tests:
            print(f"    {t.path}  ({t.name})")
    print(f"\n  stats: {json.dumps(stats)}")


def _resolve_seeds(symbols: list[Symbol], specs: list[str]) -> list[Symbol]:
    """`--changed name` or `--changed name@path` -> concrete symbols."""
    seeds: list[Symbol] = []
    for spec in specs:
        name, _, path = spec.partition("@")
        matches = [
            s for s in symbols
            if s.name == name and (not path or s.path == path or s.path.endswith(path))
        ]
        if not matches:
            print(f"  ! no symbol named {spec!r} in the index", file=sys.stderr)
            continue
        if len(matches) > 1 and not path:
            print(f"  ! {name!r} is ambiguous ({len(matches)} definitions); "
                  f"use {name}@<path>. Using all of them.", file=sys.stderr)
        seeds.extend(matches)
    return seeds


def cmd_stats(args) -> int:
    root = Path(args.path).resolve()
    extracts, symbols, edges, chunks, failures = build_index(root)
    print(json.dumps(graph.stats(symbols, edges), indent=2))
    print(f"chunks: {len(chunks)}")
    if failures and args.verbose:
        print("\nunparseable:")
        for f in failures[:40]:
            print("  " + f)
    return 0


def cmd_impact(args) -> int:
    root = Path(args.path).resolve()
    extracts, symbols, edges, chunks, _ = build_index(root)
    seeds = _resolve_seeds(symbols, args.changed)
    if not seeds:
        return 1

    vectors = query_vectors = None
    if args.semantic:
        _load_workspace_env()
        try:
            embedder = Embedder(cache_dir=root / ".zenik-cache")
            vectors = embedder.embed([c.text for c in chunks], [c.content_hash for c in chunks])
            seed_texts = []
            for s in seeds:
                fx = next((f for f in extracts if f.path == s.path), None)
                if fx:
                    lines = fx.source.decode("utf-8", "replace").split("\n")
                    seed_texts.append("\n".join(lines[s.start_line - 1 : s.end_line]))
            query_vectors = embedder.embed(seed_texts) if seed_texts else None
        except EmbeddingError as exc:
            print(f"  ! semantic layer unavailable ({exc}); edges only", file=sys.stderr)

    print(f"\nZenik — change impact for {', '.join(f'{s.name}@{s.path}' for s in seeds)}\n")
    impact, tests, stats = standalone.compute(
        symbols, edges, seeds, chunks, vectors, query_vectors,
        max_depth=args.depth, limit=args.limit,
    )
    _fmt(impact, tests, stats)
    return 0


def cmd_changed(args) -> int:
    root = Path(args.path).resolve()
    syms, snippets = changed_symbols(root, args.base, args.head)
    for s in syms:
        print(f"  {s.path}:{s.start_line}-{s.end_line}  {s.name} [{s.kind}, {s.language}]")
    print(f"\n{len(syms)} changed symbols, {len(snippets)} snippets")
    return 0


def cmd_index(args) -> int:
    _load_workspace_env()
    root = Path(args.path).resolve()
    try:
        client = PlatformClient(base_url=args.platform_url)
    except PlatformError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    embedder = None
    if not args.no_embed:
        try:
            embedder = Embedder(cache_dir=root / ".zenik-cache")
        except EmbeddingError as exc:
            print(f"error: {exc}\n(use --no-embed to index edges only)", file=sys.stderr)
            return 2

    print(f"indexing {root} as {args.repo}")
    try:
        result = push(root, args.repo, client, embedder,
                      commit_sha=args.commit or head_sha(root))
    except PlatformError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\n" + json.dumps(result.stats, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="zenik-indexer", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stats", help="parse a repo and report index shape (no network)")
    s.add_argument("path")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("impact", help="blast radius, computed in memory")
    s.add_argument("path")
    s.add_argument("--changed", nargs="+", required=True, metavar="NAME[@PATH]")
    s.add_argument("--depth", type=int, default=3)
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--semantic", action="store_true", help="also use embeddings (needs OPENAI_API_KEY)")
    s.set_defaults(func=cmd_impact)

    s = sub.add_parser("changed", help="git diff -> changed symbols")
    s.add_argument("path")
    s.add_argument("--base", default="main")
    s.add_argument("--head", default="HEAD")
    s.set_defaults(func=cmd_changed)

    s = sub.add_parser("index", help="build the index and push it to zenik-platform")
    s.add_argument("path")
    s.add_argument("--repo", required=True, metavar="owner/name")
    s.add_argument("--platform-url", default=None)
    s.add_argument("--commit", default=None)
    s.add_argument("--no-embed", action="store_true")
    s.set_defaults(func=cmd_index)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
