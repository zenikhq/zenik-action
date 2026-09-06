"""Blast-radius retrieval — fuse the deterministic graph with semantic recall.

Given the changed symbols, answer "what can this break?" by combining:

  1. A reverse walk over the edge table: who depends on the changed symbols,
     transitively, to a bounded depth. Deterministic, high precision. This is the
     recursive-CTE-over-`edges` idea (schema.sql) done in-process for the
     standalone path; the platform runs the same walk as SQL against Postgres.

  2. A vector nearest-neighbour query from the changed chunks: semantically
     related code that no import/reference edge reaches — the cross-service /
     cross-language callers that are the "easy to miss" wins. Only meaningful with
     real embeddings; with the offline hash embedder this half contributes noise,
     so it is weighted low and the deterministic half carries the answer.

The two candidate sets are fused and ranked. A missed real caller is the
dangerous error (the product promise is "we catch what you'd overlook"), so recall
is favored, but ranking pushes the provably-dependent callers to the top and caps
the total so the agent isn't handed a wall of weak matches.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional

from .edges import is_test_path
from .embeddings import Embedder, cosine
from .model import (
    ChangedSymbol, Chunk, ContextBundle, Edge, ImpactItem, IndexResult, Symbol,
    EDGE_TESTED_BY, KIND_MODULE,
)

# Ranking knobs. Deterministic edges decay with distance; semantic similarity is
# weighted below a direct edge so provable callers rank above merely-related code.
_DEPTH_DECAY = 0.6            # score multiplier per extra hop
_SEMANTIC_WEIGHT = 0.8       # scale applied to cosine similarity
_CROSS_SERVICE_BONUS = 0.15  # nudge cross-service hits up (they're the misses)
_MAX_DEPTH = 3
_MAX_SEMANTIC = 25
_MAX_IMPACT = 60


# Monorepo grouping directories: the meaningful service boundary is the segment
# BELOW these, not the grouping dir itself (services/billing-api, packages/ts-sdk).
_GROUPING_DIRS = {
    "services", "packages", "apps", "libs", "lib", "modules", "cmd",
    "internal", "pkg", "projects", "components", "src",
}


def _service_of(path: str) -> str:
    """A coarse 'service'/module boundary used to flag cross-service impact, with
    no per-repo configuration. In a monorepo laid out as services/<name>/... or
    packages/<name>/..., the boundary is the segment below the grouping dir; a
    chain of grouping dirs (e.g. services/foo/src) resolves to the first
    non-grouping segment."""
    parts = [p for p in path.split("/") if p]
    for i, seg in enumerate(parts):
        if seg not in _GROUPING_DIRS:
            return "/".join(parts[: i + 1])
    return parts[0] if parts else ""


def _common_prefix(namespaces: list[str]) -> list[str]:
    """Leading segments shared by every namespace: `com.halcyon` for a Java
    repo, `Bastion` for a C# solution. Stripped before picking the area, so
    the first *distinguishing* segment is the module."""
    split = [ns.split(".") for ns in namespaces if ns]
    if not split:
        return []
    prefix = split[0]
    for segs in split[1:]:
        n = 0
        while n < len(prefix) and n < len(segs) and prefix[n] == segs[n]:
            n += 1
        prefix = prefix[:n]
        if not prefix:
            break
    return prefix


class ImpactEngine:
    """Precomputes lookup structures over one index, then answers impact queries."""

    def __init__(self, index: IndexResult):
        self.index = index
        # File -> declared package/namespace (carried on the module symbol).
        # A monolith keeps every module under one source root, so directories
        # say nothing about boundaries there; the package does.
        self._ns_by_path: dict[str, str] = {
            s.path: s.namespace for s in index.symbols
            if s.kind == KIND_MODULE and getattr(s, "namespace", None)
        }
        distinct = sorted(set(self._ns_by_path.values()))
        self._ns_prefix = _common_prefix(distinct) if len(distinct) > 1 else []
        self.by_key: dict[str, Symbol] = {s.key(): s for s in index.symbols}
        # name/path lookups for matching changed symbols onto index symbols.
        self._keys_by_name_path: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._keys_by_path: dict[str, list[str]] = defaultdict(list)
        for s in index.symbols:
            self._keys_by_name_path[(s.path, s.name)].append(s.key())
            self._keys_by_path[s.path].append(s.key())

        # Reverse adjacency: dst -> [(src, edge_type, confidence)] = who depends on dst.
        self._rev: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        for e in index.edges:
            if e.dst:
                self._rev[e.dst].append((e.src, e.edge_type, e.confidence))

        # Chunk lookup for the semantic half.
        self._chunks: list[Chunk] = [c for c in index.chunks if c.embedding is not None]
        self._chunk_by_symbol: dict[str, Chunk] = {}
        for c in self._chunks:
            if c.symbol and c.symbol not in self._chunk_by_symbol:
                self._chunk_by_symbol[c.symbol] = c

    def _area_of(self, path: str) -> str:
        """The module a file belongs to, for cross-area flagging. Namespace
        first (first segment after the repo-wide prefix), directory otherwise."""
        ns = self._ns_by_path.get(path)
        if not ns:
            return _service_of(path)
        segs = ns.split(".")
        n = len(self._ns_prefix)
        rest = segs[n:] if segs[:n] == self._ns_prefix else segs
        return "ns:" + (rest[0] if rest else segs[-1])

    # -- seed resolution ----------------------------------------------------
    def _resolve_seeds(self, changed: list[ChangedSymbol]) -> dict[str, str]:
        """Map each changed symbol to index symbol key(s). Returns key -> the
        changed symbol's display name it traces to (for `via`)."""
        seeds: dict[str, str] = {}
        for cs in changed:
            keys = self._keys_by_name_path.get((cs.path, cs.name))
            if keys and len(keys) > 1:
                # A name can legitimately be defined more than once in a file —
                # Go's `type Estimate struct` next to `func (e *X) Estimate()`
                # is the common case. Prefer the definition(s) the changed
                # lines actually touch, so a method edit doesn't also seed the
                # unrelated type and drag in every consumer of it.
                overlapping = [k for k in keys if self._overlaps(self.by_key[k], cs)]
                if overlapping:
                    keys = overlapping
            if not keys:
                # Fall back to any symbol in the same file overlapping the lines,
                # else the file's module symbol — never lose a changed file.
                overlap = [
                    k for k in self._keys_by_path.get(cs.path, [])
                    if self._overlaps(self.by_key[k], cs)
                ]
                keys = overlap or self._keys_by_path.get(cs.path, [])
            for k in keys:
                seeds.setdefault(k, cs.name)
        return seeds

    @staticmethod
    def _overlaps(sym: Symbol, cs: ChangedSymbol) -> bool:
        return not (sym.end_line < cs.start_line or sym.start_line > cs.end_line)

    # -- deterministic reverse walk ----------------------------------------
    def _walk(self, seeds: dict[str, str], max_depth: int
             ) -> dict[str, tuple[int, str, float, set[str]]]:
        """BFS over reverse edges. Returns key -> (depth, reason, confidence, vias)."""
        best: dict[str, tuple[int, str, float, set[str]]] = {}
        # queue holds (key, depth, via_name)
        q: deque[tuple[str, int, str]] = deque((k, 0, v) for k, v in seeds.items())
        seed_keys = set(seeds)
        while q:
            key, depth, via = q.popleft()
            if depth >= max_depth:
                continue
            for src, etype, conf in self._rev.get(key, []):
                if src in seed_keys:
                    continue  # a changed symbol isn't its own impact
                ndepth = depth + 1
                prev = best.get(src)
                if prev is None or ndepth < prev[0]:
                    vias = set(prev[3]) if prev else set()
                    vias.add(via)
                    best[src] = (ndepth, etype, conf, vias)
                    q.append((src, ndepth, via))
                else:
                    prev[3].add(via)
        return best

    # -- semantic NN --------------------------------------------------------
    def _semantic(self, seeds: dict[str, str], top_k: int,
                  query_vectors: Optional[dict[str, list[float]]] = None,
                 ) -> dict[str, tuple[float, set[str]]]:
        """Cosine-NN from each seed's chunk to all chunks. Returns key -> (sim, vias).

        `query_vectors` (changed-symbol NAME -> embedding) lets the caller
        supply a FRESH vector for the changed code — a PR query embeds only its
        changed chunks and searches the stored graph with them. Falls back to
        the stored chunk's (base-version) embedding when absent.
        """
        if not self._chunks:
            return {}
        stored_dim = len(self._chunks[0].embedding or [])
        out: dict[str, tuple[float, set[str]]] = {}
        for seed_key, via in seeds.items():
            qchunk = self._chunk_by_symbol.get(seed_key)
            qvec = (query_vectors or {}).get(via)
            if qvec is not None and len(qvec) != stored_dim:
                qvec = None  # different embedder than the stored graph — unusable
            if qvec is None:
                qvec = qchunk.embedding if qchunk is not None else None
            if qvec is None:
                continue
            qsym = self.by_key.get(seed_key)
            qpath = qsym.path if qsym is not None else (
                qchunk.path if qchunk is not None else None)
            sims: list[tuple[float, Chunk]] = []
            for c in self._chunks:
                if c.symbol == seed_key or c.path == qpath:
                    continue  # skip self and same-file chunks
                sims.append((cosine(qvec, c.embedding), c))
            sims.sort(key=lambda t: t[0], reverse=True)
            for sim, c in sims[:top_k]:
                if sim <= 0 or not c.symbol:
                    continue
                cur = out.get(c.symbol)
                if cur is None or sim > cur[0]:
                    vias = set(cur[1]) if cur else set()
                    vias.add(via)
                    out[c.symbol] = (sim, vias)
                else:
                    cur[1].add(via)
        return out

    # -- fuse ---------------------------------------------------------------
    def compute(
        self,
        changed: list[ChangedSymbol],
        *,
        max_depth: int = _MAX_DEPTH,
        semantic: bool = True,
        top_semantic: int = _MAX_SEMANTIC,
        query_vectors: Optional[dict[str, list[float]]] = None,
    ) -> ContextBundle:
        seeds = self._resolve_seeds(changed)
        changed_services = {self._area_of(cs.path) for cs in changed}

        det = self._walk(seeds, max_depth)
        sem = (self._semantic(seeds, top_semantic, query_vectors)
               if semantic else {})

        candidates = set(det) | set(sem)
        items: list[ImpactItem] = []
        for key in candidates:
            sym = self.by_key.get(key)
            if sym is None or sym.kind == KIND_MODULE:
                # Module symbols are graph glue, not something a reviewer acts on;
                # surface real definitions only.
                continue

            score = 0.0
            reason = "semantic"
            depth = 0
            confidence = 0.0
            vias: set[str] = set()

            if key in det:
                d, etype, conf, dvias = det[key]
                depth, reason, confidence = d, etype, conf
                score += conf * (_DEPTH_DECAY ** (d - 1))
                vias |= dvias
            if key in sem:
                sim, svias = sem[key]
                score += sim * _SEMANTIC_WEIGHT
                vias |= svias
                if key not in det:
                    confidence = round(sim, 3)

            cross = self._area_of(sym.path) not in changed_services
            if cross:
                score += _CROSS_SERVICE_BONUS

            items.append(ImpactItem(
                symbol=sym, reason=reason, depth=depth,
                confidence=round(confidence, 3), cross_service=cross,
                score=round(score, 4), via=sorted(vias),
            ))

        items.sort(key=lambda it: it.score, reverse=True)
        # Cap the two kinds separately. Semantic look-alikes are collected per
        # seed (top_semantic each), so a PR with five seeds arrives with over a
        # hundred of them and they were crowding provable depth-2/3 callers out
        # of one shared cap. Provable edges get the full cap; look-alikes get
        # one budget across all seeds. `truncated` speaks only for the provable
        # list — a reviewer is never told "there may be more" about guesses.
        provable = [it for it in items if it.reason != "semantic"]
        lookalike = [it for it in items if it.reason == "semantic"]
        truncated = len(provable) > _MAX_IMPACT
        items = provable[:_MAX_IMPACT] + lookalike[:top_semantic]

        # Split tests out — a change's relevant tests are surfaced on their own
        # (the `tested_by` reason or a test-looking path), not buried in callers.
        tests = [it for it in items
                 if it.reason == EDGE_TESTED_BY or is_test_path(it.symbol.path)]
        impacted = [it for it in items if it not in tests]

        return ContextBundle(
            changed=changed, impacted=impacted, tests=tests, truncated=truncated,
        )


def compute_impact(
    index: IndexResult,
    changed: list[ChangedSymbol],
    *,
    max_depth: int = _MAX_DEPTH,
    semantic: bool = True,
) -> ContextBundle:
    """Convenience wrapper: build an engine and run one query."""
    return ImpactEngine(index).compute(
        changed, max_depth=max_depth, semantic=semantic,
    )
