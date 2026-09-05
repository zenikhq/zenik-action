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
definitions AFTER import scoping has narrowed the candidates) are skipped — they
would explode the graph without adding certainty.
"""
from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

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
        self._by_key: dict[str, Symbol] = {}
        for s in symbols:
            self._by_key[s.key()] = s
            if s.kind != KIND_MODULE:
                self._by_name[s.name].append(s)

    def resolve(self, name: str) -> list[Symbol]:
        return self._by_name.get(name, [])

    def path_of(self, key: str) -> str:
        s = self._by_key.get(key)
        return s.path if s is not None else ""

    def symbol_of(self, key: str) -> Symbol | None:
        return self._by_key.get(key)


def _file_of(key: str) -> str:
    """Symbol.key() is 'path::name::line' — recover the path."""
    return key.split("::", 1)[0]


def _dir_of(path: str) -> str:
    return posixpath.dirname(path)


def _dir_ends_with(d: str, tail: str) -> bool:
    return d == tail or d.endswith("/" + tail)


# ---------------------------------------------------------------------------
# Import scoping for languages whose parser leaves `RawImport.items` empty.
#
# tree-sitter-language-pack's process() populates `items` for Python/TypeScript
# but returns Java and Go imports with only their `source` text, and returns no
# imports at all for C# (extract.py reads C# `using` directives and the declared
# namespace from the source text instead). Scoping edges purely on `items`
# therefore discards every cross-file reference in those languages. The helpers
# below derive scope from `source` plus the repo's directory / namespace layout,
# table-driven on Symbol.language (never on file extension).
# ---------------------------------------------------------------------------

class _Layout:
    """Layout of the indexed repo, from the per-file module symbols: language
    per path, files per directory, files per declared namespace, and the
    suffix lookups used to map a Java package / Go import path / C# namespace
    onto the files that make it up."""

    def __init__(self, symbols: list[Symbol], go_module: str | None = None):
        self.language_of: dict[str, str] = {}
        self.files_by_dir: dict[str, set[str]] = defaultdict(set)
        self.namespace_of: dict[str, str] = {}
        self.files_by_namespace: dict[str, set[str]] = defaultdict(set)
        # Module path from the repo's go.mod (`module github.com/x/repo`), or
        # None when there is none. With it, only imports under that path can
        # claim a repo directory.
        self.go_module: str | None = (go_module or "").strip() or None
        # The three suffix lookups below scan every directory / namespace per
        # call; imports repeat across files (every Go file imports the same
        # handful of packages), so each is memoized on its argument.
        self._tail_cache: dict[str, set[str]] = {}
        self._suffix_cache: dict[str, set[str]] = {}
        self._ns_tree_cache: dict[str, set[str]] = {}
        for s in symbols:
            if s.kind == KIND_MODULE:
                self.language_of[s.path] = s.language
                self.files_by_dir[_dir_of(s.path)].add(s.path)
                if s.namespace:
                    self.namespace_of[s.path] = s.namespace
                    self.files_by_namespace[s.namespace].add(s.path)

    def files(self) -> list[str]:
        return list(self.language_of)

    def siblings(self, path: str) -> set[str]:
        return self.files_by_dir.get(_dir_of(path), set()) - {path}

    def namespace_peers(self, path: str) -> set[str]:
        """Other files declaring exactly the same namespace as `path`, wherever
        they live; empty when `path` declares none."""
        ns = self.namespace_of.get(path)
        if not ns:
            return set()
        return self.files_by_namespace.get(ns, set()) - {path}

    def files_in_namespace_tree(self, namespace: str) -> set[str]:
        """Files declaring `namespace` or any sub-namespace of it. For C# this
        is a deliberate over-approximation in favour of recall: a file with
        `using A.B;` is usually working with A.B's sub-namespaces too (which
        see A.B's types without any using of their own). A namespace no file
        declares — System.*, NuGet packages — yields nothing."""
        if not namespace:
            return set()
        cached = self._ns_tree_cache.get(namespace)
        if cached is None:
            cached = set()
            prefix = namespace + "."
            for ns, files in self.files_by_namespace.items():
                if ns == namespace or ns.startswith(prefix):
                    cached |= files
            self._ns_tree_cache[namespace] = cached
        return set(cached)

    def files_in_dirs_ending_with(self, tail: str) -> set[str]:
        """Files in every directory equal to `tail` or ending in '/<tail>'
        (Java: package com.x.orders -> src/main/java/com/x/orders, and the
        matching src/test/java/... directory)."""
        if not tail:
            return set()
        cached = self._tail_cache.get(tail)
        if cached is None:
            cached = set()
            for d, files in self.files_by_dir.items():
                if _dir_ends_with(d, tail):
                    cached |= files
            self._tail_cache[tail] = cached
        return set(cached)

    def files_in_dir_suffix_of(self, import_path: str) -> set[str]:
        """Files in the LONGEST repo directory that is a trailing segment run of
        `import_path` (Go: "github.com/x/portage/internal/routing" ->
        internal/routing). Longest wins so a top-level `routing/` does not also
        claim `internal/routing`. Standard-library and third-party paths match no
        repo directory and yield nothing.

        When go.mod names the module, only an import path equal to it or
        under it is considered at all: `github.com/other/lib/internal/routing`
        must not claim this repo's internal/routing, nor
        `github.com/rs/zerolog/log` a top-level log/. Without a go.mod the
        suffix match stands alone (the pre-go.mod behaviour)."""
        if not import_path:
            return set()
        cached = self._suffix_cache.get(import_path)
        if cached is None:
            cached = set()
            if self._under_go_module(import_path):
                best: str | None = None
                for d in self.files_by_dir:
                    if d and _dir_ends_with(import_path, d):
                        if best is None or len(d) > len(best):
                            best = d
                if best is not None:
                    cached = set(self.files_by_dir[best])
            self._suffix_cache[import_path] = cached
        return set(cached)

    def _under_go_module(self, import_path: str) -> bool:
        m = self.go_module
        return m is None or import_path == m or import_path.startswith(m + "/")


@dataclass
class _DerivedScope:
    """What a `source`-only import contributes to scoping.

    `names` are simple names, resolved exactly as `RawImport.items` would be, each
    with an optional directory tail: when some resolution lives in a directory
    ending in that tail, only those resolutions count (a Java `Order` imported from
    com.x.orders.domain must not scope an unrelated `Order` elsewhere).
    `dir_tails` put every file of a package (directory ending in the tail) in
    scope; `import_paths` put every file of the Go package at that path in scope;
    `namespaces` put every file declaring the C# namespace, or a sub-namespace
    of it, in scope.
    """
    names: list[tuple[str, str | None]] = field(default_factory=list)
    dir_tails: list[str] = field(default_factory=list)
    import_paths: list[str] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)

    def paths(self, layout: _Layout) -> set[str]:
        out: set[str] = set()
        for tail in self.dir_tails:
            out |= layout.files_in_dirs_ending_with(tail)
        for p in self.import_paths:
            out |= layout.files_in_dir_suffix_of(p)
        for ns in self.namespaces:
            out |= layout.files_in_namespace_tree(ns)
        return out


_JAVA_IMPORT_RX = re.compile(
    r"^\s*import\s+(?P<static>static\s+)?(?P<dotted>[\w$]+(?:\s*\.\s*[\w$]+)*)"
    r"(?P<wild>\s*\.\s*\*)?\s*;?\s*$"
)


def _java_scope(imp: RawImport) -> _DerivedScope:
    """`import a.b.C;`          -> name C, preferring definitions under a/b
       `import static a.b.C.m;` -> names C and m, preferring a/b
       `import a.b.*;`          -> every file in a directory ending in a/b
       `import static a.b.C.*;` -> name C, preferring a/b"""
    m = _JAVA_IMPORT_RX.match(imp.source)
    if not m:
        return _DerivedScope()
    segments = [s.strip() for s in m.group("dotted").split(".") if s.strip()]
    if not segments:
        return _DerivedScope()
    static = bool(m.group("static"))
    wildcard = imp.is_wildcard or bool(m.group("wild"))
    out = _DerivedScope()
    if wildcard:
        if static:
            out.names.append((segments[-1], "/".join(segments[:-1]) or None))
        else:
            out.dir_tails.append("/".join(segments))
    elif static and len(segments) >= 2:
        pkg = "/".join(segments[:-2]) or None
        out.names.append((segments[-2], pkg))
        out.names.append((segments[-1], pkg))
    else:
        out.names.append((segments[-1], "/".join(segments[:-1]) or None))
    return out


def _go_scope(imp: RawImport) -> _DerivedScope:
    """`"github.com/x/repo/internal/routing"` -> every file in the repo directory
    that is the longest trailing run of that path. The alias (`r "…"`, `_`, `.`)
    changes how the package is spelled at use sites, not which files are in
    scope, so it is stripped: `source` may arrive as `r "github.com/…"`, and
    the path is always its last whitespace-separated token."""
    tokens = imp.source.split()
    path = tokens[-1].strip('"`').strip() if tokens else ""
    out = _DerivedScope()
    if path:
        out.import_paths.append(path)
    return out


def _csharp_scope(imp: RawImport) -> _DerivedScope:
    """`using A.B;` (also `global using`) -> every file declaring namespace A.B
    or a sub-namespace of it. extract.py stores the bare dotted namespace in
    `source`. `using static A.B.C;` and `using X = A.B.C;` arrive with
    `items=["C"]` and never reach here: they import one type, not a namespace."""
    ns = imp.source.strip().rstrip(";").strip()
    out = _DerivedScope()
    if ns:
        out.namespaces.append(ns)
    return out


# language -> deriver applied to imports whose `items` is empty.
_SCOPE_DERIVERS: dict[str, Callable[[RawImport], _DerivedScope]] = {
    "java": _java_scope,
    "go": _go_scope,
    "csharp": _csharp_scope,
}

# Languages where every file in a directory shares one package and so references
# sibling files with no import at all.
_SAME_DIR_LANGUAGES = frozenset({"java", "go"})

# Languages where the unit of bare visibility is the DECLARED namespace, wherever
# the files live: two C# files declaring `namespace A.B;` in different
# directories (or different projects) see each other with no `using`; a Java
# `package com.x;` under src/main/java and the same package under
# src/test/java are one package. The namespace comes from the module symbol
# (extract.py); a file that declares none falls back to same-directory
# scoping as the last resort. Java is in both sets: the directory rule covers
# the default package, the namespace rule covers split source roots.
_SAME_NAMESPACE_LANGUAGES = frozenset({"csharp", "java"})


def build_edges(
    symbols: list[Symbol],
    file_imports: list[tuple[str, list[RawImport]]],   # (module_key, imports)
    file_refs: list[RawRef],
    *,
    scoped_references: bool = True,
    go_module: str | None = None,
) -> list[Edge]:
    """Construct the full edge list. Deduplicates on (src, dst, edge_type),
    keeping the strongest confidence.

    `go_module` is the module path from the repo's go.mod, when it has one;
    Go import scoping then only considers imports under that path.

    `scoped_references` (default on) is the precision control for the reference
    layer: a cross-file reference edge is kept only when the referencing file
    actually imports a name defined in the target's file. Same-file references are
    always kept. This removes the dominant false positive of name-based edges — a
    parameter/local/field that merely shares a name with a definition in an
    unrelated file (e.g. every `currency` param resolving to one `currency`
    property). Real call edges survive because callers import what they call;
    genuine cross-service links are recovered by the semantic layer, not here.

    How a file's scope (the set of files it may reference) is built:

      1. `RawImport.items` — the imported names, resolved against the symbol
         table (Python, TypeScript). Unchanged.
      2. When `items` is empty, a per-language deriver (`_SCOPE_DERIVERS`, keyed
         on the module symbol's language) reads `RawImport.source`: Java yields
         the simple name(s) and, for a wildcard, the package's files; Go yields
         the files of the repo directory the import path ends in; C# yields the
         files declaring the `using` namespace or any sub-namespace of it
         (`using static` / alias forms carry the type name in `items` instead).
      3. Same-package rule: Java and Go files in one directory are one package
         and reference each other bare (`_SAME_DIR_LANGUAGES`); C# files
         declaring the same namespace, and Java files declaring the same
         package (src/main/java vs src/test/java), are in scope for each
         other regardless of directory (`_SAME_NAMESPACE_LANGUAGES`,
         namespace from the module symbol), with same-directory only for a
         file that declares no namespace at all.
      4. Deliberate failure mode — never drop everything: a file in a language
         WITH a deriver whose imports are non-empty but produced no scope at all
         (every `items` empty and the deriver found nothing) falls back to
         same-directory scoping rather than losing every cross-file edge. Files
         whose imports parsed but resolved only to external packages do NOT
         fall back: that is a real, empty scope. Languages without a deriver
         (Python, TypeScript) never fall back: an empty `items` there is the
         parser's real answer for default / namespace / side-effect / `*`
         imports, not a gap in understanding.
    """
    resolver = _Resolver(symbols)
    layout = _Layout(symbols, go_module=go_module)
    # (src, dst, type) -> Edge, so we can keep the highest-confidence duplicate.
    best: dict[tuple[str, str | None, str], Edge] = {}
    # file path -> set of target file paths it may reference across files.
    imported_files: dict[str, set[str]] = defaultdict(set)

    def _add(src: str, dst_key: str | None, dst_name: str | None,
             edge_type: str, confidence: float):
        k = (src, dst_key, edge_type)
        cur = best.get(k)
        if cur is None or confidence > cur.confidence:
            best[k] = Edge(src=src, dst=dst_key, dst_name=dst_name,
                           edge_type=edge_type, confidence=round(confidence, 3))

    def _import_name(module_key: str, src_file: str, name: str,
                     dir_tail: str | None) -> None:
        """Resolve one imported name: scope the target files and emit an
        `imports` edge per definition. `dir_tail` narrows resolutions to the
        package directory the import names, when any definition lives there."""
        targets = resolver.resolve(name)
        if not targets:
            return  # external dep
        if dir_tail:
            in_pkg = [t for t in targets if _dir_ends_with(_dir_of(t.path), dir_tail)]
            if in_pkg:
                targets = in_pkg
        # The ambiguity cap applies AFTER package narrowing: `import a.b.C`
        # names one package's C, however many other packages define a C.
        if len(targets) > MAX_RESOLUTIONS:
            return  # too ambiguous even within the named package
        conf = 1.0 / len(targets)
        for t in targets:
            if t.key() == module_key:
                continue
            imported_files[src_file].add(t.path)
            _add(module_key, t.key(), name, EDGE_IMPORTS, conf)

    # --- import edges (also builds the import scope map) --------------------
    for module_key, imports in file_imports:
        src_file = _file_of(module_key)
        language = layout.language_of.get(src_file, "")
        derive = _SCOPE_DERIVERS.get(language)
        parsed_any = False
        for imp in imports:
            if imp.items:
                parsed_any = True
                for name in imp.items:
                    _import_name(module_key, src_file, name, None)
                continue
            if derive is None:
                continue
            derived = derive(imp)
            for name, tail in derived.names:
                parsed_any = True
                _import_name(module_key, src_file, name, tail)
            paths = derived.paths(layout)
            if paths:
                parsed_any = True
                imported_files[src_file] |= paths - {src_file}
        # Rule 4: imports present but none understood -> same-directory scope.
        # Only for languages with a deriver: their parser leaves `items` empty
        # by design, so "nothing understood" means the deriver failed. For
        # Python/TypeScript an empty `items` is a real answer (`import x from
        # "express"`, `import * as ns`, side-effect imports, `from x import *`)
        # and falling back would re-link every same-directory name collision.
        if (derive is not None and imports and not parsed_any
                and not imported_files.get(src_file)):
            imported_files[src_file] |= layout.siblings(src_file)

    # Rule 3: one package per directory (Java/Go); one namespace per declared
    # namespace (C#, Java), same-directory only when the file declares none.
    for path in layout.files():
        language = layout.language_of[path]
        if language in _SAME_DIR_LANGUAGES:
            imported_files[path] |= layout.siblings(path)
        if language in _SAME_NAMESPACE_LANGUAGES:
            if layout.namespace_of.get(path):
                imported_files[path] |= layout.namespace_peers(path)
            else:
                imported_files[path] |= layout.siblings(path)

    # --- reference / call / tested_by edges --------------------------------
    for ref in file_refs:
        targets = resolver.resolve(ref.name)
        if not targets:
            continue
        src_sym = resolver.symbol_of(ref.src)
        # The identifier harvest yields a definition's OWN name on its
        # declaration line (`constructor(...) {`, `def save(`). That is not a
        # reference to the other `constructor`s / `save`s in scope; the
        # repo-wide ambiguity cap used to hide most of these by accident.
        if (src_sym is not None and src_sym.name == ref.name
                and ref.line == src_sym.start_line):
            continue
        src_path = src_sym.path if src_sym is not None else ""
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
        # The ambiguity cap applies to what SCOPING left, not to the repo-wide
        # count: `save` / `findById` / `Validate` are defined on a dozen
        # repository interfaces, but a file that imports one of them means
        # exactly that one. Capping before scoping made every common method
        # name resolve to nothing.
        if not eligible or len(eligible) > MAX_RESOLUTIONS:
            continue
        conf = 1.0 / len(eligible)
        for t in eligible:
            etype = EDGE_CALLS_MAYBE if ref.is_call else EDGE_REFERENCES
            _add(ref.src, t.key(), ref.name, etype, conf)
            # A test referencing a non-test symbol is a tested_by edge too.
            if src_is_test and not is_test_path(t.path):
                _add(ref.src, t.key(), ref.name, EDGE_TESTED_BY, conf)

    return list(best.values())
