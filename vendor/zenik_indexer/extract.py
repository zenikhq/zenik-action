"""Parse one file into symbols, imports and reference sites.

Three things come out of a single parse:

  * **symbols** — definitions, from ``tlp.process``. Uniform across grammars.
  * **imports** — module-level dependencies, also from ``tlp.process``.
  * **references** — every identifier used inside a symbol's body, harvested
    directly from the syntax tree.

The reference harvest deliberately does *not* use the grammars' ``tags``
queries. Coverage there is uneven in exactly the wrong places: TypeScript's tags
query has no ``@reference.call`` capture at all, and C has no reference captures
whatsoever — so a tags-based extractor would silently return zero callers for
two major languages. Harvesting identifiers and resolving them against the
repo's defined-symbol set is what BUILD.md §2 Layer B actually specifies
("reference sites → candidate call edges by name"), and it degrades uniformly
rather than falling off a cliff per language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_language_pack as tlp
from tree_sitter_language_pack import ProcessConfig

from .langs import detect

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Node types that carry an identifier across grammars. Anything whose type
# *contains* "identifier" qualifies; these are the extra spellings that don't.
EXTRA_IDENT_TYPES = {"name", "constant", "word", "symbol", "label"}

# A reference sitting under one of these ancestors is a call, not just a
# mention. Method calls nest one level deeper (obj.method() parses as
# identifier -> attribute/selector -> call), so ancestors are checked to depth 3.
CALLISH = ("call", "invocation", "new_expression", "macro_invocation")

# Identifiers that are language noise, never a meaningful edge target.
STOPWORDS = {
    "self", "this", "cls", "super", "true", "false", "null", "nil", "none",
    "undefined", "string", "int", "bool", "float", "error", "err", "e", "i",
    "j", "k", "x", "y", "n", "_", "args", "kwargs", "value", "data", "result",
    "type", "new", "len", "print", "console", "log", "return",
}


@dataclass
class Symbol:
    name: str
    kind: str
    path: str
    start_line: int  # 1-based
    end_line: int
    language: str


@dataclass
class Import:
    raw: str
    items: list[str]
    path: str
    line: int


@dataclass
class Reference:
    name: str
    path: str
    line: int
    in_symbol: str | None  # name of the enclosing symbol
    is_call: bool


@dataclass
class FileExtract:
    path: str
    language: str
    source: bytes
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    error: str | None = None

    @property
    def module_symbol(self) -> Symbol:
        """A synthetic symbol standing for the file itself.

        File-level imports have no enclosing function to hang off, and the
        reverse walk needs a real ``src_symbol_id`` for every edge. Giving each
        file a module symbol keeps the graph connected and makes "this file
        depends on X" walkable like any other edge.
        """
        line_count = self.source.count(b"\n") + 1
        return Symbol(
            name=Path(self.path).name,
            kind="module",
            path=self.path,
            start_line=1,
            end_line=line_count,
            language=self.language,
        )


def _kind_of(sym) -> str:
    raw = str(getattr(sym, "kind", "other"))
    return raw.rsplit(".", 1)[-1].lower()


def _is_callish(node) -> bool:
    cur = node.parent
    for _ in range(3):
        if cur is None:
            return False
        if any(tok in cur.type for tok in CALLISH):
            return True
        cur = cur.parent
    return False


def _enclosing(symbols: list[Symbol], line: int) -> str | None:
    """Innermost symbol whose span covers ``line``."""
    best: Symbol | None = None
    for s in symbols:
        if s.start_line <= line <= s.end_line:
            if best is None or (s.end_line - s.start_line) < (best.end_line - best.start_line):
                best = s
    return best.name if best else None


def _harvest_references(
    source: bytes, language: str, symbols: list[Symbol], rel_path: str
) -> list[Reference]:
    try:
        parser = tlp.get_parser(language)
        tree = parser.parse(source)
    except Exception:
        return []

    out: list[Reference] = []
    seen: set[tuple[str, int]] = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.named_child_count == 0 and node.is_named:
            ntype = node.type
            if "identifier" in ntype or ntype in EXTRA_IDENT_TYPES:
                text = source[node.start_byte : node.end_byte].decode("utf-8", "replace")
                if IDENT_RE.match(text) and text.lower() not in STOPWORDS and len(text) > 2:
                    line = node.start_point[0] + 1
                    if (text, line) not in seen:
                        seen.add((text, line))
                        out.append(
                            Reference(
                                name=text,
                                path=rel_path,
                                line=line,
                                in_symbol=_enclosing(symbols, line),
                                is_call=_is_callish(node),
                            )
                        )
        stack.extend(node.children)
    return out


def extract_file(abs_path: Path, rel_path: str) -> FileExtract | None:
    language = detect(abs_path)
    if not language:
        return None
    try:
        source = abs_path.read_bytes()
    except OSError as exc:
        return FileExtract(path=rel_path, language=language, source=b"", error=str(exc))

    fx = FileExtract(path=rel_path, language=language, source=source)
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        fx.error = "not utf-8"
        return fx

    try:
        result = tlp.process(
            text, ProcessConfig(language=language, symbols=True, imports=True)
        )
    except Exception as exc:
        fx.error = f"{type(exc).__name__}: {exc}"
        return fx

    for s in result.symbols or []:
        span = s.span
        fx.symbols.append(
            Symbol(
                name=s.name,
                kind=_kind_of(s),
                path=rel_path,
                start_line=span.start_line + 1,  # tree-sitter spans are 0-based
                end_line=span.end_line + 1,
                language=language,
            )
        )

    for i in result.imports or []:
        fx.imports.append(
            Import(
                raw=i.source or "",
                items=list(i.items or []),
                path=rel_path,
                line=(i.span.start_line + 1) if i.span else 1,
            )
        )

    fx.references = _harvest_references(source, language, fx.symbols, rel_path)
    return fx
