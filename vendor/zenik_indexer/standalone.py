"""Blast radius computed entirely in memory — no Supabase, no CI, no network
unless embeddings are requested.

BUILD.md §3 is explicit that the indexer must run standalone against a real repo
first, so accuracy can be judged as a black box before anything is wired up.
This mirrors the platform's fuse (services/impact.py) closely enough that a
disagreement between the two is a bug in one of them — which makes it a useful
cross-check, not just a convenience.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from .chunker import Chunk
from .extract import Symbol
from .graph import Edge, TEST_PATH_RE

EDGE_DECAY = 0.6
WEIGHT_EDGES = 0.65
WEIGHT_VECTOR = 0.35

# Prune thresholds — must stay in step with zenik-platform's config.py, since
# the whole point of this module is that the two agree.
VECTOR_FLOOR_CORROBORATED = 0.30
VECTOR_FLOOR_ALONE = 0.55
STRUCTURAL_KINDS = {"module"}


@dataclass
class Hit:
    name: str
    path: str
    language: str
    kind: str
    start_line: int
    end_line: int
    depth: int | None
    via: list[str]
    sources: list[str]
    similarity: float | None
    score: float
    is_test: bool
    cross_language: bool


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def reverse_walk(
    edges: list[Edge], seeds: list[Symbol], max_depth: int = 3
) -> dict[tuple[str, str], tuple[int, list[str], float]]:
    """{(path, name): (depth, via, confidence)} for everything that depends on a seed.

    Follows both resolved edges (dst_path set) and name-only edges, exactly as
    the platform's recursive CTE does.
    """
    by_dst_key: dict[tuple[str, str], list[Edge]] = defaultdict(list)
    by_dst_name: dict[str, list[Edge]] = defaultdict(list)
    for e in edges:
        if e.dst_path:
            by_dst_key[(e.dst_path, e.dst_name)].append(e)
        else:
            by_dst_name[e.dst_name].append(e)

    found: dict[tuple[str, str], tuple[int, list[str], float]] = {}
    frontier = [((s.path, s.name), 1.0, []) for s in seeds]
    visited = {k for k, _, _ in frontier}

    for depth in range(1, max_depth + 1):
        nxt = []
        for (path, name), conf, via in frontier:
            incoming = by_dst_key.get((path, name), []) + by_dst_name.get(name, [])
            for e in incoming:
                key = (e.src_path, e.src_name)
                if key in visited:
                    continue
                new_conf = conf * e.confidence
                new_via = via + [e.edge_type]
                prev = found.get(key)
                if prev is None or depth < prev[0]:
                    found[key] = (depth, new_via, new_conf)
                visited.add(key)
                nxt.append((key, new_conf, new_via))
        frontier = nxt
        if not frontier:
            break
    return found


def compute(
    symbols: list[Symbol],
    edges: list[Edge],
    seeds: list[Symbol],
    chunks: list[Chunk] | None = None,
    vectors: list[list[float]] | None = None,
    query_vectors: list[list[float]] | None = None,
    max_depth: int = 3,
    limit: int = 25,
) -> tuple[list[Hit], list[Hit], dict]:
    sym_by_key = {(s.path, s.name): s for s in symbols}
    seed_keys = {(s.path, s.name) for s in seeds}
    seed_langs = {s.language for s in seeds}

    def is_the_change(sym: Symbol) -> bool:
        for s in seeds:
            if s.path == sym.path and sym.start_line <= s.start_line and sym.end_line >= s.end_line:
                return True
        return False

    merged: dict[tuple[str, str], dict] = {}

    for key, (depth, via, conf) in reverse_walk(edges, seeds, max_depth).items():
        sym = sym_by_key.get(key)
        if sym is None or key in seed_keys or is_the_change(sym):
            continue
        if sym.kind in STRUCTURAL_KINDS:
            continue  # walked through the file symbol; it is not itself a finding
        merged[key] = {
            "sym": sym, "depth": depth, "via": via, "sources": ["edges"],
            "sim": None, "_edge": conf * (EDGE_DECAY ** max(0, depth - 1)), "_vec": 0.0,
        }

    if chunks and vectors and query_vectors:
        best: dict[tuple[str, str], float] = {}
        for chunk, vec in zip(chunks, vectors):
            if not chunk.symbol_name:
                continue
            key = (chunk.path, chunk.symbol_name)
            sim = max(_cosine(q, vec) for q in query_vectors)
            if sim > best.get(key, -1.0):
                best[key] = sim
        for key, sim in sorted(best.items(), key=lambda kv: -kv[1])[: limit * 3]:
            sym = sym_by_key.get(key)
            if sym is None or key in seed_keys or is_the_change(sym):
                continue
            # Prune: corroborated hits may sit lower, vector-only must clear more.
            floor = VECTOR_FLOOR_CORROBORATED if key in merged else VECTOR_FLOOR_ALONE
            if sim < floor:
                continue
            if key not in merged and sym.kind in STRUCTURAL_KINDS:
                continue
            if key in merged:
                merged[key]["_vec"] = sim
                merged[key]["sim"] = sim
                merged[key]["sources"].append("vector")
            else:
                merged[key] = {
                    "sym": sym, "depth": None, "via": [], "sources": ["vector"],
                    "sim": sim, "_edge": 0.0, "_vec": sim,
                }

    hits = [
        Hit(
            name=m["sym"].name, path=m["sym"].path, language=m["sym"].language,
            kind=m["sym"].kind, start_line=m["sym"].start_line, end_line=m["sym"].end_line,
            depth=m["depth"], via=m["via"], sources=m["sources"], similarity=m["sim"],
            score=round(WEIGHT_EDGES * m["_edge"] + WEIGHT_VECTOR * m["_vec"], 6),
            is_test=bool(TEST_PATH_RE.search(m["sym"].path)),
            cross_language=bool(
                m["sym"].language and seed_langs and m["sym"].language not in seed_langs
            ),
        )
        for m in merged.values()
    ]
    hits.sort(key=lambda h: h.score, reverse=True)

    impact = [h for h in hits if not h.is_test][:limit]
    tests = [h for h in hits if h.is_test][:limit]
    stats = {
        "candidates": len(hits),
        "returned": len(impact) + len(tests),
        "cross_language_hits": sum(1 for h in impact if h.cross_language),
        "languages": sorted({h.language for h in impact if h.language}),
        "edges_only": sum(1 for h in impact if h.sources == ["edges"]),
        "vector_only": sum(1 for h in impact if h.sources == ["vector"]),
        "both": sum(1 for h in impact if len(h.sources) == 2),
    }
    return impact, tests, stats
