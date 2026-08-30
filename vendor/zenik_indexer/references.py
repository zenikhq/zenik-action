"""Reference/call harvest — the half of Layer B that `process()` does not give.

Verified finding (see the tree-sitter memory): the grammars' `tags` queries do
NOT uniformly emit call sites — TypeScript has no `@reference.call` at all, C has
no reference captures, C# uses a different name. A tags-based extractor silently
returns zero callers for whole languages and looks like it works.

So we harvest references ourselves and degrade uniformly instead:

  1. Greedily collect every identifier-ish node from the syntax tree.
  2. Attribute each to its innermost enclosing defined symbol (by line span).
  3. Flag call-vs-mention by walking up a few ancestors for a call/invocation node.

The precision step — keeping only names that match a symbol DEFINED in this repo —
happens in edges.py, where the repo-wide symbol table is known. Here we just emit
candidate references; the intersection is what turns noise into signal.
"""
from __future__ import annotations

from dataclasses import dataclass

from tree_sitter_language_pack import get_parser

from .model import Symbol

# Node types that name something. "identifier" as a substring covers the bulk
# (identifier, type_identifier, property_identifier, field_identifier,
# package_identifier, scoped_identifier, ...); the rest catch language-specific
# spellings (Ruby `constant`/`symbol`, some grammars' `name`/`word`/`label`).
_EXTRA_NAME_TYPES = {"name", "constant", "symbol", "word", "label"}

# Ancestor node types (matched as substrings) that mean "the identifier is being
# called/instantiated", not merely mentioned. Method calls nest, so we look up a
# few levels: `obj.m()` is identifier -> attribute/selector -> call.
_CALL_ANCESTOR_HINTS = ("call", "invocation", "new_expression", "new_object")
_CALL_LOOKUP_DEPTH = 3


@dataclass
class RawRef:
    """A candidate reference from one enclosing symbol to a bare name."""
    src: str          # Symbol.key() of the enclosing definition (or module)
    name: str         # the referenced identifier text
    is_call: bool
    line: int         # 1-based


def _is_name_node(node) -> bool:
    t = node.type
    return ("identifier" in t) or (t in _EXTRA_NAME_TYPES)


def _looks_like_call(node) -> bool:
    anc = node
    for _ in range(_CALL_LOOKUP_DEPTH):
        anc = anc.parent
        if anc is None:
            return False
        if any(h in anc.type for h in _CALL_ANCESTOR_HINTS):
            return True
    return False


def _enclosing_key(line: int, spans: list[tuple[int, int, str]], module_key: str) -> str:
    """Innermost symbol whose [start,end] contains `line`; module if none.

    `spans` is pre-sorted so that narrower spans win (a method inside a class is
    attributed to the method, not the class)."""
    best = module_key
    best_width = None
    for start, end, key in spans:
        if start <= line <= end:
            width = end - start
            if best_width is None or width < best_width:
                best_width = width
                best = key
    return best


def harvest_references(
    source_bytes: bytes,
    language: str,
    file_symbols: list[Symbol],
    module_key: str,
) -> list[RawRef]:
    """Return every candidate reference in one file, attributed to its enclosing
    symbol. Names are NOT yet filtered to repo-defined symbols — edges.py does
    that intersection."""
    try:
        parser = get_parser(language)
        tree = parser.parse(source_bytes)
    except Exception as e:  # noqa: BLE001 - unparseable file: no refs rather than a crash
        print(f"[references] parse failed for {language} ({type(e).__name__}); no refs")
        return []

    # Spans to attribute against — real definitions only (not the module symbol,
    # which spans the whole file and would swallow everything).
    spans = [(s.start_line, s.end_line, s.key()) for s in file_symbols]

    refs: list[RawRef] = []
    # Explicit stack rather than recursion: source trees can be deep enough to
    # blow Python's recursion limit on large minified-ish files.
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if _is_name_node(node) and node.child_count == 0:
            name = source_bytes[node.start_byte:node.end_byte].decode("utf-8", "replace")
            if name:
                line = node.start_point[0] + 1
                refs.append(RawRef(
                    src=_enclosing_key(line, spans, module_key),
                    name=name,
                    is_call=_looks_like_call(node),
                    line=line,
                ))
        # Push children (order doesn't matter — we attribute by line span).
        stack.extend(node.children)

    return refs
