"""Edge construction — the precision layer.

Two deterministic edge sources, both resolved against the repo-wide symbol table:

  * imports  — file (module symbol) -> each imported name that resolves to a
    symbol DEFINED in this repo. External imports (os, fmt, lodash) resolve to
    nothing and are dropped: they carry no in-repo blast radius.

  * references / calls_maybe — enclosing symbol -> each referenced name that
    resolves to a repo symbol. `calls_maybe` when the identifier sits under a
    call node, `references` otherwise. This is where the harvest's noise gets
    filtered: only names that match a real definition survive.

  * tested_by — additionally emitted when the referencing symbol lives in a test
    file, so impact.py can surface "tests likely relevant to this change" for free.

Ambiguity is handled by confidence, not by dropping signal: a name that resolves
to one definition is confidence 1.0; a name with N definitions splits confidence
across them. Pathologically common names (resolving to more than MAX_RESOLUTIONS
definitions) are skipped — they would explode the graph without adding certainty.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .extract import RawImport
from .model import (
    Edge, Symbol,
    EDGE_IMPORTS, EDGE_REFERENCES, EDGE_CALLS_MAYBE, EDGE_TESTED_BY,
    KIND_MODULE,
)
from .references import RawRef

# A name resolving to more definitions than this is treated as too-ambiguous to
# be a useful edge (e.g. a `handler` defined in 40 files). Keeps the graph from
# blowing up on common names.
MAX_RESOLUTIONS = 8

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)spec/"),
    re.compile(r"(^|/)test_[^/]+$"),
    re.compile(r"_test\.[^/.]+$"),
    re.compile(r"\.(test|spec)\.[^/.]+$"),
    re.compile(r"Tests?\.[^/.]+$"),
    re.compile(r"_spec\.[^/.]+$"),
)


def is_test_path(path: str) -> bool:
    p = path.lower()
    return any(rx.search(p) for rx in _TEST_PATTERNS)


class _Resolver:
    """Maps a bare name to the repo symbols defining it (module symbols excluded —
    their names are file paths and never match an imported/referenced identifier)."""

    def __init__(self, symbols: list[Symbol]):
        self._by_name: dict[str, list[Symbol]] = defaultdict(list)
        self._path_of: dict[str, str] = {}
        for s in symbols:
            self._path_of[s.key()] = s.path
            if s.kind != KIND_MODULE:
                self._by_name[s.name].append(s)

    def resolve(self, name: str) -> list[Symbol]:
        return self._by_name.get(name, [])

    def path_of(self, key: str) -> str:
        return self._path_of.get(key, "")


def _file_of(key: str) -> str:
    """Symbol.key() is 'path::name::line' — recover the path."""
    return key.split("::", 1)[0]


def build_edges(
    symbols: list[Symbol],
    file_imports: list[tuple[str, list[RawImport]]],   # (module_key, imports)
    file_refs: list[RawRef],
    *,
    scoped_references: bool = True,
) -> list[Edge]:
    """Construct the full edge list. Deduplicates on (src, dst, edge_type),
    keeping the strongest confidence.

    `scoped_references` (default on) is the precision control for the reference
    layer: a cross-file reference edge is kept only when the referencing file
    actually imports a name defined in the target's file. Same-file references are
    always kept. This removes the dominant false positive of name-based edges — a
    parameter/local/field that merely shares a name with a definition in an
    unrelated file (e.g. every `currency` param resolving to one `currency`
    property). Real call edges survive because callers import what they call;
    genuine cross-service links are recovered by the semantic layer, not here.
    """
    resolver = _Resolver(symbols)
    # (src, dst, type) -> Edge, so we can keep the highest-confidence duplicate.
    best: dict[tuple[str, str | None, str], Edge] = {}
    # file path -> set of target file paths it imports a resolvable name from.
    imported_files: dict[str, set[str]] = defaultdict(set)

    def _add(src: str, dst_key: str | None, dst_name: str | None,
             edge_type: str, confidence: float):
        k = (src, dst_key, edge_type)
        cur = best.get(k)
        if cur is None or confidence > cur.confidence:
            best[k] = Edge(src=src, dst=dst_key, dst_name=dst_name,
                           edge_type=edge_type, confidence=round(confidence, 3))

    # --- import edges (also builds the import scope map) --------------------
    for module_key, imports in file_imports:
        src_file = _file_of(module_key)
        for imp in imports:
            for name in imp.items:
                targets = resolver.resolve(name)
                if not targets or len(targets) > MAX_RESOLUTIONS:
                    continue  # external dep or too ambiguous
                conf = 1.0 / len(targets)
                for t in targets:
                    if t.key() == module_key:
                        continue
                    imported_files[src_file].add(t.path)
                    _add(module_key, t.key(), name, EDGE_IMPORTS, conf)

    # --- reference / call / tested_by edges --------------------------------
    for ref in file_refs:
        targets = resolver.resolve(ref.name)
        if not targets or len(targets) > MAX_RESOLUTIONS:
            continue
        src_path = resolver.path_of(ref.src)
        src_is_test = is_test_path(src_path) if src_path else False
        scope = imported_files.get(src_path, frozenset())
        # Split confidence only across targets that survive scoping, so a real
        # single in-file/imported target keeps full confidence.
        eligible = [
            t for t in targets
            if t.key() != ref.src and (
                not scoped_references or t.path == src_path or t.path in scope
            )
        ]
        if not eligible:
            continue
        conf = 1.0 / len(eligible)
        for t in eligible:
            etype = EDGE_CALLS_MAYBE if ref.is_call else EDGE_REFERENCES
            _add(ref.src, t.key(), ref.name, etype, conf)
            # A test referencing a non-test symbol is a tested_by edge too.
            if src_is_test and not is_test_path(t.path):
                _add(ref.src, t.key(), ref.name, EDGE_TESTED_BY, conf)

    return list(best.values())
