"""Per-file extraction — Layer A chunks + the definition/import half of Layer B.

Everything here comes from tree-sitter-language-pack's `process()`, which yields
symbols (definitions), imports, exports, and a nesting-aware `structure` uniformly
across languages. We do NOT use its size-based `chunks`: Zenik chunks along symbol
boundaries (whole function/class), which `structure` gives us directly.

Reference/call edges are NOT produced here — `process()` does not emit call sites.
They come from references.py's raw-tree identifier harvest.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import tree_sitter_language_pack as tlp

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


def _kind_to_str(kind) -> str:
    """Normalize a tree-sitter SymbolKind to the plain string schema.sql expects
    (function | class | method | interface | type | enum | constant | variable |
    module | other). SymbolKind stringifies as e.g. 'Class'/'Function'."""
    name = getattr(kind, "name", None) or str(kind)
    return name.split(".")[-1].strip().lower() or "other"


def _module_name(rel_path: str) -> str:
    return rel_path


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
        source = source_bytes.decode("utf-8", errors="replace")
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
    )

    # --- definitions -------------------------------------------------------
    symbols: list[Symbol] = []
    for sym in (res.symbols or []):
        sp = sym.span
        symbols.append(Symbol(
            name=sym.name,
            kind=_kind_to_str(sym.kind),
            path=rel_path,
            language=language,
            start_line=sp.start_line + 1,
            end_line=sp.end_line + 1,
            commit_sha=commit_sha,
        ))

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

    return FileExtract(
        module_symbol=module_symbol,
        symbols=symbols,
        chunks=chunks,
        imports=imports,
    )
