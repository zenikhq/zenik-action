"""Per-file extraction — Layer A chunks + the definition/import half of Layer B.

Everything here comes from tree-sitter-language-pack's `process()`, which yields
symbols (definitions), imports, exports, and a nesting-aware `structure` uniformly
across languages. We do NOT use its size-based `chunks`: Zenik chunks along symbol
boundaries (whole function/class), which `structure` gives us directly.

Reference/call edges are NOT produced here — `process()` does not emit call sites.
They come from references.py's raw-tree identifier harvest.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import tree_sitter_language_pack as tlp
from tree_sitter_language_pack import get_parser

from .model import Chunk, Symbol, content_hash, KIND_MODULE


@dataclass
class RawImport:
    """A file-level import, straight from process(). Resolved into edges later,
    once every file's symbols are known (edges.py)."""
    items: list[str]         # imported names, e.g. ["charge"] or [] for `import "fmt"`
    source: str              # raw import text, e.g. 'from billing.core import charge'
    alias: Optional[str]
    is_wildcard: bool
    line: int                # 1-based


@dataclass
class FileExtract:
    module_symbol: Symbol
    symbols: list[Symbol]        # definitions, EXCLUDING the module symbol
    chunks: list[Chunk]
    imports: list[RawImport]
    # Declared namespace (C# `namespace`, Java `package`; None elsewhere). Also
    # carried on `module_symbol.namespace` so edges.py sees it through the
    # symbol table.
    namespace: Optional[str] = None


def _kind_to_str(kind) -> str:
    """Normalize a tree-sitter SymbolKind to the plain string schema.sql expects
    (function | class | method | interface | type | enum | constant | variable |
    module | other). SymbolKind stringifies as e.g. 'Class'/'Function'."""
    name = getattr(kind, "name", None) or str(kind)
    return name.split(".")[-1].strip().lower() or "other"


# `structure` kinds -> symbols.kind. `process()`'s flat `symbols` list is
# top-level-only for Java/C# (no methods at all) and drops Go receiver methods,
# but the nesting-aware `structure` tree has them as children. StructureKind
# has a few members SymbolKind lacks; map them onto the schema's vocabulary.
# Kinds mapped to None are containers, not definitions: we still descend into
# them (an `impl` block's methods are real symbols) but emit nothing for them.
# `module` here is Java's `package x;` line — the synthetic module symbol
# already covers the file.
_STRUCTURE_KIND_MAP: dict[str, Optional[str]] = {
    "function": "function",
    "method": "method",
    "class": "class",
    "interface": "interface",
    "trait": "interface",
    "enum": "enum",
    "struct": "type",
    "impl": None,
    "module": None,
    "namespace": None,
    "other": None,
}


def _walk_structure(
    items,
    rel_path: str,
    language: str,
    commit_sha: Optional[str],
) -> list[Symbol]:
    """Depth-first harvest of `structure` into a flat list of Symbols, in source
    order. `Symbol.name` stays the BARE identifier (edges.py's resolver and the
    identifier harvest match on bare names); the enclosing type, if any, goes in
    `Symbol.parent`. Iterative so a deeply nested file can't hit the recursion
    limit."""
    out: list[Symbol] = []
    stack = [(item, None) for item in reversed(list(items or []))]
    while stack:
        item, enclosing = stack.pop()
        kind = _STRUCTURE_KIND_MAP.get(_kind_to_str(item.kind))
        name = (getattr(item, "name", None) or "").strip()
        sp = item.span
        child_parent = enclosing
        if kind is not None and name:
            out.append(Symbol(
                name=name,
                kind=kind,
                path=rel_path,
                language=language,
                start_line=sp.start_line + 1,
                end_line=sp.end_line + 1,
                commit_sha=commit_sha,
                parent=enclosing,
            ))
            child_parent = name
        children = getattr(item, "children", None) or []
        stack.extend((c, child_parent) for c in reversed(list(children)))
    return out

# ---------------------------------------------------------------------------
# Record declarations. tree-sitter-language-pack's `structure` does not know
# Java/C# `record`s: a positional record (`record Consent(String id)`,
# `record HazardGrade(int Value);`) yields nothing at all, and a C# `record
# class { ... }` surfaces as an unnamed `Class` item that _walk_structure has
# to drop. Its methods do come through, but with no enclosing type, so they
# land at file level with `parent=None`. _STRUCTURE_KIND_MAP cannot fix a node
# that is never emitted, so records are harvested from the raw syntax tree.
# Records are classes in both languages; they use the schema's `class` kind
# (a C# `record struct` too — keep one kind per concept, not per keyword).
# ---------------------------------------------------------------------------

_RECORD_LANGUAGES = {"java", "csharp"}

# body member node type -> symbols.kind, per language. C# `property_declaration`
# is deliberately absent: schema.sql has no property kind and the pack emits no
# properties for ordinary classes either, so records stay consistent with them.
# Java's compact canonical constructor (`public Consent { ... }`) IS harvested:
# it is the one body a validating record almost always has, and a diff inside
# it should resolve to that block rather than the whole record.
_RECORD_MEMBER_KINDS: dict[str, dict[str, str]] = {
    # The compact canonical constructor is deliberately NOT harvested: it
    # shares the record's bare name, and edges.py resolves by bare name, so a
    # reference to `Consent` would split its confidence between the type and
    # a constructor nobody calls by name.
    "java": {"method_declaration": "method"},
    "csharp": {"method_declaration": "method"},
}

# Type declarations that give a nested record its `parent`.
_RECORD_ENCLOSING_TYPES = {
    "class_declaration", "interface_declaration", "enum_declaration",
    "record_declaration", "struct_declaration",
}


def _node_name(node, source_bytes: bytes) -> str:
    n = node.child_by_field_name("name")
    if n is None:
        n = next((c for c in node.children if c.type == "identifier"), None)
    if n is None:
        return ""
    return source_bytes[n.start_byte:n.end_byte].decode("utf-8", "replace").strip()


def _harvest_records(
    source_bytes: bytes,
    rel_path: str,
    language: str,
    commit_sha: Optional[str],
) -> list[Symbol]:
    """Records (as `class`) plus their body methods (as `method`, with
    `parent=<record>`) from the raw tree-sitter tree, in source order. Empty
    for languages without a `record_declaration` node.

    This parses the file a second time: tlp.process() parses internally but
    does not expose its Tree, and references.py builds its own as well. A
    parse is cheap next to process(); sharing one tree across the three
    would be a wider refactor than this gap warrants.
    """
    member_kinds = _RECORD_MEMBER_KINDS.get(language)
    if not member_kinds:
        return []
    try:
        tree = get_parser(language).parse(source_bytes)
    except Exception as e:  # noqa: BLE001 - keep the pack's symbols; just no records
        print(f"[extract] {rel_path}: record harvest parse failed ({type(e).__name__})")
        return []

    out: list[Symbol] = []
    stack = [(tree.root_node, None)]
    while stack:
        node, enclosing = stack.pop()
        child_enclosing = enclosing
        if node.type == "record_declaration":
            name = _node_name(node, source_bytes)
            if name:
                out.append(Symbol(
                    name=name, kind="class", path=rel_path, language=language,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    commit_sha=commit_sha, parent=enclosing,
                ))
                child_enclosing = name
                body = node.child_by_field_name("body")
                for member in (body.children if body is not None else []):
                    kind = member_kinds.get(member.type)
                    if kind is None:
                        continue
                    mname = _node_name(member, source_bytes)
                    if not mname:
                        continue
                    out.append(Symbol(
                        name=mname, kind=kind, path=rel_path, language=language,
                        start_line=member.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        commit_sha=commit_sha, parent=name,
                    ))
        elif node.type in _RECORD_ENCLOSING_TYPES:
            child_enclosing = _node_name(node, source_bytes) or enclosing
        # reversed so pop() yields source order
        stack.extend((c, child_enclosing) for c in reversed(node.children))
    return out


def _module_name(rel_path: str) -> str:
    return rel_path


# ---------------------------------------------------------------------------
# C# directives. tree-sitter-language-pack's process() returns NO imports for
# C#, so `using` directives and the declared namespace are read from the
# source text. The scanner is deliberately conservative: it walks only the
# file's leading lines (blank, comment, preprocessor, attribute, `extern
# alias`, `using`, `namespace`) and stops at the first line that is anything
# else — the first type declaration or top-level statement. A `using (var x
# = ...)` / `using var x = ...` statement sits inside a method body, after
# that point, and is never reached; both forms are also rejected outright.
# ---------------------------------------------------------------------------

_CS_USING_RX = re.compile(
    r"^\s*(?:global\s+)?using\s+(?P<static>static\s+)?"
    r"(?:(?P<alias>[A-Za-z_]\w*)\s*=\s*)?"
    r"(?:global\s*::\s*)?"
    r"(?P<dotted>[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*;"
)
# No `$` anchor: `namespace A.B { class X {} }` on one line and
# `namespace A.B // comment` both declare A.B.
_CS_NAMESPACE_RX = re.compile(
    r"^\s*(?:file\s+)?namespace\s+(?P<dotted>[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)(?![\w.])"
)
# Any other `using ...;` / `namespace ...` line is a directive we do not model
# (generic alias `using L = List<int>;`, tuple alias `using P = (int x, int
# y);`) — skipped, never a reason to stop scanning.
_CS_DIRECTIVE_LINE_RX = re.compile(r"^\s*(?:global\s+)?(?:using|namespace)\b")
# Lines that may precede or sit between directives without ending the scan.
_CS_SKIP_RX = re.compile(r"^\s*(?://|#|\[|extern\s+alias\b|\{|\}|$)")
# `using var x = ...;` and `using (var x = ...)` are statements, not directives.
_CS_USING_STATEMENT_RX = re.compile(r"^\s*using\s*(?:\(|var\b)")


def _csharp_directives(source: str) -> tuple[list[RawImport], Optional[str]]:
    """Parse `using` directives and the declared namespace from a C# file.

    Returns (imports, namespace). Handled forms and how they land in RawImport:

      using A.B.C;              source="A.B.C", items=[]        (namespace import;
                                edges.py derives scope from `source`)
      global using A.B.C;       same as above
      using static A.B.C;       source="A.B.C", items=["C"]     (brings C's members
                                into scope; C is a type, resolved like an item)
      using X = A.B.C;          source="A.B.C", items=["C"], alias="X"
                                (X names the type C in this file; the type's
                                bare name is what the symbol table resolves)

    For `using static` and alias forms `items` is the LAST segment only, so the
    namespace `A.B` is NOT put in scope — matching C# semantics, where neither
    form imports the enclosing namespace. An alias of a namespace rather than a
    type (`using P = A.B;`) yields the item "B", which resolves to nothing
    unless a type of that name exists; it does not scope A.B.

    Namespace: the first `namespace A.B.C;` (file-scoped) or `namespace A.B.C {`
    seen. Limitation: a file with several block-form namespaces records only
    the first; the rest of the file is scoped as if it were in that namespace.
    """
    imports: list[RawImport] = []
    namespace: Optional[str] = None
    in_block_comment = False
    for lineno, raw in enumerate(source.splitlines(), start=1):
        line = raw
        if in_block_comment:
            end = line.find("*/")
            if end < 0:
                continue
            in_block_comment = False
            line = line[end + 2:]
        stripped = line.strip()
        if stripped.startswith("/*"):
            end = stripped.find("*/", 2)
            if end < 0:
                in_block_comment = True
                continue
            stripped = stripped[end + 2:].strip()
            line = stripped
        if _CS_USING_STATEMENT_RX.match(line):
            break
        m = _CS_USING_RX.match(line)
        if m:
            dotted = re.sub(r"\s+", "", m.group("dotted"))
            segments = dotted.split(".")
            static = bool(m.group("static"))
            alias = m.group("alias")
            items = [segments[-1]] if (static or alias) else []
            imports.append(RawImport(
                items=items, source=dotted, alias=alias,
                is_wildcard=False, line=lineno,
            ))
            continue
        m = _CS_NAMESPACE_RX.match(line)
        if m:
            if namespace is None:
                namespace = re.sub(r"\s+", "", m.group("dotted"))
            continue
        if _CS_SKIP_RX.match(line) or _CS_DIRECTIVE_LINE_RX.match(line):
            continue
        break  # first type declaration / top-level statement: directives are over
    return imports, namespace


# ---------------------------------------------------------------------------
# Java: the `package a.b.c;` declaration. process() already yields Java
# imports (as `source`-only RawImports); only the package is read here, so
# edges.py can treat files of one package as one scope wherever they live
# (src/main/java/com/x and src/test/java/com/x are the same package).
# ---------------------------------------------------------------------------

_JAVA_PACKAGE_RX = re.compile(
    r"^\s*package\s+(?P<dotted>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*;",
    re.MULTILINE,
)


def _java_package(source: str) -> tuple[list[RawImport], Optional[str]]:
    """(imports, namespace) for Java: imports are always empty here (process()
    provides them); namespace is the declared package, or None for the default
    package. First match wins; a `package` inside a comment before the real
    one is the only way to fool it, which no real file does."""
    m = _JAVA_PACKAGE_RX.search(source)
    if not m:
        return [], None
    return [], re.sub(r"\s+", "", m.group("dotted"))


# language -> source-text directive reader, for languages whose process()
# output carries no import data at all.
_DIRECTIVE_READERS = {
    "csharp": _csharp_directives,
    "java": _java_package,
}


def extract_file(
    rel_path: str,
    source_bytes: bytes,
    language: str,
    commit_sha: Optional[str] = None,
) -> Optional[FileExtract]:
    """Extract symbols, chunks, and imports for one file.

    `rel_path` is the repo-relative POSIX path (used verbatim as symbols.path).
    Returns None if the file can't be parsed at all.
    """
    try:
        # utf-8-sig: a leading U+FEFF (Visual Studio writes one by default)
        # would otherwise survive as line 1's first character and defeat every
        # `^\s*` anchor in the directive readers.
        source = source_bytes.decode("utf-8-sig", errors="replace")
    except Exception:
        return None

    cfg = tlp.ProcessConfig(
        language=language,
        symbols=True,
        imports=True,
        exports=True,
        structure=True,
    )
    try:
        res = tlp.process(source, cfg)
    except Exception as e:  # noqa: BLE001 - a single unparseable file must not abort the index
        print(f"[extract] {rel_path}: process failed ({type(e).__name__}); skipping")
        return None

    total_lines = source.count("\n") + 1

    # Languages whose process() output carries no import data: read the
    # directives (and the declared namespace) from the source text instead.
    directive_imports: list[RawImport] = []
    namespace: Optional[str] = None
    reader = _DIRECTIVE_READERS.get(language)
    if reader is not None:
        directive_imports, namespace = reader(source)

    # Synthetic module symbol: gives file-level imports a real `src` anchor and
    # keeps the graph connected even for files with no top-level definitions.
    module_symbol = Symbol(
        name=_module_name(rel_path),
        kind=KIND_MODULE,
        path=rel_path,
        language=language,
        start_line=1,
        end_line=total_lines,
        commit_sha=commit_sha,
        namespace=namespace,
    )

    # --- definitions -------------------------------------------------------
    # Two sources, unioned:
    #   * `res.symbols` — flat. Complete for Python/TS-style languages, but for
    #     Java/C# it holds only top-level types (no methods) and for Go it
    #     drops receiver methods. It carries kinds `structure` lacks (Go `type`,
    #     constants/variables), so it stays the primary source.
    #   * `res.structure` — nesting-aware; has the methods as children.
    # Dedupe on (name, 1-based start_line): the one identity both fields agree
    # on (Python/TS symbols appear in both with identical spans). A definition
    # in both is emitted once, with `res.symbols`' kind.
    structure_syms = _walk_structure(
        getattr(res, "structure", None), rel_path, language, commit_sha)
    parent_of = {(s.name, s.start_line): s.parent for s in structure_syms}

    symbols: list[Symbol] = []
    seen: set[tuple[str, int]] = set()
    for sym in (res.symbols or []):
        sp = sym.span
        key = (sym.name, sp.start_line + 1)
        if key in seen:
            continue
        seen.add(key)
        symbols.append(Symbol(
            name=sym.name,
            kind=_kind_to_str(sym.kind),
            path=rel_path,
            language=language,
            start_line=sp.start_line + 1,
            end_line=sp.end_line + 1,
            commit_sha=commit_sha,
            parent=parent_of.get(key),
        ))
    for s in structure_syms:
        key = (s.name, s.start_line)
        if key not in seen:
            seen.add(key)
            symbols.append(s)
    #   * records (Java/C#) — from the raw tree, see _harvest_records. Same
    #     dedupe: a record the pack did name is not doubled.
    record_syms = _harvest_records(source_bytes, rel_path, language, commit_sha) \
        if language in _RECORD_LANGUAGES else []
    for s in record_syms:
        key = (s.name, s.start_line)
        if key not in seen:
            seen.add(key)
            symbols.append(s)
    # The pack emitted a record's methods without an enclosing type: give them
    # the record as `parent`. Exact (name, line) matches first; any other
    # parentless METHOD inside a record span (a member kind the harvest doesn't
    # list) gets the innermost enclosing record. Types are left alone: an outer
    # class and its one-line nested record share a line span, and the pack
    # already parents nested types correctly.
    if record_syms:
        records = [s for s in record_syms if s.kind == "class"]
        member_parent = {(s.name, s.start_line): s.parent
                         for s in record_syms if s.kind != "class"}
        for sym in symbols:
            key = (sym.name, sym.start_line)
            if key in member_parent:
                sym.parent = member_parent[key]
                continue
            if sym.parent is not None or sym.kind not in ("method", "function"):
                continue
            inner = None
            for r in records:
                if (r.name, r.start_line) == key:
                    continue
                if r.start_line <= sym.start_line and sym.end_line <= r.end_line:
                    if inner is None or (r.end_line - r.start_line) < (inner.end_line - inner.start_line):
                        inner = r
            if inner is not None:
                sym.parent = inner.name

    # --- chunks (whole top-level function/class spans) ---------------------
    # `structure` is nesting-aware; its top-level items are exactly the
    # function/class boundaries we want to embed. Methods live inside their
    # class's chunk, so we do NOT descend — one chunk per top-level unit.
    src_lines = source.splitlines()
    chunks: list[Chunk] = []
    covered_any = False
    for item in (getattr(res, "structure", None) or []):
        sp = item.span
        start = sp.start_line + 1
        end = sp.end_line + 1
        text = "\n".join(src_lines[sp.start_line: sp.end_line + 1])
        if not text.strip():
            continue
        covered_any = True
        # Tie the chunk to its definition symbol when names/lines line up.
        sym_key = None
        for s in symbols:
            if s.name == item.name and s.start_line == start:
                sym_key = s.key()
                break
        chunks.append(Chunk(
            path=rel_path,
            language=language,
            start_line=start,
            end_line=end,
            content_hash=content_hash(text),
            symbol=sym_key,
            text=text,
        ))

    # A file with no top-level structure (config-ish, scripts, top-level-only
    # code) still carries meaning — embed it whole so semantic recall isn't blind
    # to it. Skip genuinely empty files.
    if not covered_any and source.strip():
        chunks.append(Chunk(
            path=rel_path,
            language=language,
            start_line=1,
            end_line=total_lines,
            content_hash=content_hash(source),
            symbol=module_symbol.key(),
            text=source,
        ))

    # --- imports -----------------------------------------------------------
    imports: list[RawImport] = []
    for imp in (res.imports or []):
        imports.append(RawImport(
            items=list(getattr(imp, "items", None) or []),
            source=getattr(imp, "source", "") or "",
            alias=getattr(imp, "alias", None),
            is_wildcard=bool(getattr(imp, "is_wildcard", False)),
            line=imp.span.start_line + 1,
        ))
    # process() is the primary source; the text reader only fills the gap when
    # it returned nothing (C# today), so a parser that starts emitting imports
    # for the language is picked up automatically.
    if not imports:
        imports = directive_imports

    return FileExtract(
        module_symbol=module_symbol,
        symbols=symbols,
        chunks=chunks,
        imports=imports,
        namespace=namespace,
    )
