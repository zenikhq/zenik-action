"""Turn per-file extracts into the symbol + edge tables (BUILD.md §2 Layer B).

Resolution is by name against the repo's own defined-symbol set, which is what
makes this work without a resolver per language. Three outcomes per reference:

  * resolves to exactly one definition  -> a resolved edge, high confidence
  * resolves to several definitions     -> a *name-only* edge (``dst_name``),
    lower confidence. The platform's recursive CTE follows these by name, so
    ambiguity is carried to query time instead of being guessed at index time.
  * resolves to nothing                 -> dropped. It was a local, a keyword,
    or a third-party symbol we do not index.

That last case is the precision mechanism: the identifier harvest is
deliberately greedy, and intersecting with real definitions is what discards the
noise.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .extract import FileExtract, Symbol

TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|spec|specs|__tests__|testdata)(/|$)"
    r"|(^|/)[^/]*(_test|_spec|\.test|\.spec)\.[A-Za-z]+$",
    re.IGNORECASE,
)

CONFIDENCE = {
    "imports_resolved": 0.9,
    "imports_named": 0.5,
    "calls_resolved": 0.8,
    "calls_named": 0.5,
    "references_resolved": 0.6,
    "references_named": 0.35,
    "tested_by": 0.85,
}


@dataclass
class Edge:
    src_path: str
    src_name: str
    dst_path: str | None
    dst_name: str
    edge_type: str
    confidence: float


def _is_test(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def build(extracts: list[FileExtract]) -> tuple[list[Symbol], list[Edge]]:
    symbols: list[Symbol] = []
    by_name: dict[str, list[Symbol]] = defaultdict(list)

    for fx in extracts:
        if fx.error:
            continue
        module = fx.module_symbol
        symbols.append(module)
        for s in fx.symbols:
            symbols.append(s)
            by_name[s.name].append(s)

    edges: list[Edge] = []
    seen: set[tuple] = set()

    def emit(src_path, src_name, dst: Symbol | None, dst_name, etype, conf) -> None:
        if dst is not None and dst.path == src_path and dst.name == src_name:
            return  # self-edge
        key = (src_path, src_name, dst.path if dst else None, dst_name, etype)
        if key in seen:
            return
        seen.add(key)
        edges.append(
            Edge(
                src_path=src_path,
                src_name=src_name,
                dst_path=dst.path if dst else None,
                dst_name=dst_name,
                edge_type=etype,
                confidence=conf,
            )
        )

    def resolve(name: str, from_path: str) -> tuple[Symbol | None, bool]:
        """(symbol, unique). Prefers a definition in another file — a reference
        to a name defined locally is usually the definition itself, not a call
        across the codebase."""
        candidates = by_name.get(name, [])
        if not candidates:
            return None, False
        external = [c for c in candidates if c.path != from_path]
        pool = external or candidates
        return (pool[0], True) if len(pool) == 1 else (None, False)

    for fx in extracts:
        if fx.error:
            continue
        module_name = fx.module_symbol.name
        src_is_test = _is_test(fx.path)

        # --- imports: module -> imported definition ------------------------
        for imp in fx.imports:
            for item in imp.items:
                if item not in by_name:
                    continue  # third-party or stdlib; not ours to track
                dst, unique = resolve(item, fx.path)
                if unique and dst:
                    emit(fx.path, module_name, dst, item, "imports",
                         CONFIDENCE["imports_resolved"])
                else:
                    emit(fx.path, module_name, None, item, "imports",
                         CONFIDENCE["imports_named"])

        # --- references: enclosing symbol -> referenced definition ---------
        for ref in fx.references:
            if ref.name not in by_name:
                continue
            src_name = ref.in_symbol or module_name
            dst, unique = resolve(ref.name, fx.path)

            if src_is_test:
                etype, conf_key = "tested_by", "tested_by"
            elif ref.is_call:
                etype = "calls_maybe"
                conf_key = "calls_resolved" if unique else "calls_named"
            else:
                etype = "references"
                conf_key = "references_resolved" if unique else "references_named"

            emit(fx.path, src_name, dst if unique else None, ref.name,
                 etype, CONFIDENCE[conf_key])

    return symbols, edges


def stats(symbols: list[Symbol], edges: list[Edge]) -> dict:
    by_type: dict[str, int] = defaultdict(int)
    resolved = 0
    for e in edges:
        by_type[e.edge_type] += 1
        if e.dst_path:
            resolved += 1
    langs: dict[str, int] = defaultdict(int)
    for s in symbols:
        langs[s.language] += 1
    return {
        "symbols": len(symbols),
        "edges": len(edges),
        "edges_by_type": dict(by_type),
        "edges_resolved": resolved,
        "edges_name_only": len(edges) - resolved,
        "languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])),
    }
