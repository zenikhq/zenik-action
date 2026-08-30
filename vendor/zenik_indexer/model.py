"""Core data contracts for the indexer.

These dataclasses are the shapes the rest of Zenik conforms to:

  * `Symbol` / `Edge` / `Chunk` mirror the `symbols` / `edges` / `chunks` tables
    in `schema.sql`. What the indexer produces is exactly what the platform
    persists — no translation layer.
  * `ChangedSymbol` is the CROSS-REPO CONTRACT. `zenik-action` computes a list
    of these from a PR diff and POSTs them to `zenik-platform`; the indexer's
    `impact` uses the same shape. Keep it stable.
  * `ContextBundle` is what a blast-radius query returns — the "context bundle"
    the action hands to the coding agent.

Line numbers are 1-based and inclusive everywhere in Zenik (matching git and
human intuition). tree-sitter spans are 0-based; the conversion happens once, at
extraction time (see extract.py), so nothing downstream deals in 0-based lines.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Enumerated string constants (kept as plain strings to match the text columns
# in schema.sql; no DB enum type is imposed).
# ---------------------------------------------------------------------------

# symbols.kind — superset of tree-sitter SymbolKind plus our synthetic "module".
KIND_MODULE = "module"

# edges.edge_type
EDGE_IMPORTS = "imports"            # file-level dependency (deterministic)
EDGE_REFERENCES = "references"      # name mention of a repo symbol (not a call)
EDGE_CALLS_MAYBE = "calls_maybe"    # candidate call site by name (may be ambiguous)
EDGE_TESTED_BY = "tested_by"        # a test symbol references this symbol

# ChangedSymbol.change_type
CHANGE_ADDED = "added"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"


def content_hash(text: str) -> str:
    """Stable content hash for a chunk, used for incremental re-embedding.

    A chunk whose `content_hash` is unchanged since the last index does not need
    re-embedding — that is the hook schema.sql's `chunks.content_hash` exists for.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


@dataclass
class Symbol:
    """A definition: function, class, method, exported name, or a synthetic
    per-file `module` symbol (so file-level imports have a real source anchor
    and the graph stays connected)."""
    name: str
    kind: str
    path: str                    # repo-relative POSIX path
    language: str
    start_line: int              # 1-based, inclusive
    end_line: int                # 1-based, inclusive
    commit_sha: Optional[str] = None
    # Assigned by the store when persisted; the in-memory index uses a local id.
    id: Optional[str] = None

    def key(self) -> str:
        """Stable identity within a repo, independent of the store's uuid.

        Two indexes of the same commit produce the same keys, which is what lets
        impact history line up across runs.
        """
        return f"{self.path}::{self.name}::{self.start_line}"

    def to_row(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "language": self.language,
            "commit_sha": self.commit_sha,
        }


@dataclass
class Edge:
    """A dependency edge. `src`/`dst` are Symbol.key() references within one
    index; `dst` may be None when the edge is name-only (unresolved)."""
    src: str                     # Symbol.key() of the depender
    edge_type: str
    dst: Optional[str] = None    # Symbol.key() of the dependency, if resolved
    dst_name: Optional[str] = None   # bare name when unresolved / ambiguous
    confidence: float = 1.0


@dataclass
class Chunk:
    """A semantic chunk (whole function/class/method span, or a whole small
    file) plus its embedding. `symbol` ties it back to a definition when the
    chunk is a single symbol's body."""
    path: str
    language: str
    start_line: int
    end_line: int
    content_hash: str
    symbol: Optional[str] = None       # Symbol.key(), when chunk == one symbol
    text: Optional[str] = None         # kept in-memory only; NEVER persisted
    embedding: Optional[list[float]] = None


# ---------------------------------------------------------------------------
# Cross-repo contract: a changed symbol from a PR diff.
# ---------------------------------------------------------------------------
@dataclass
class ChangedSymbol:
    """One symbol touched by a PR diff. This is the payload `zenik-action`
    computes and sends to `zenik-platform` to seed a blast-radius query."""
    name: str
    kind: str
    path: str
    language: str
    start_line: int
    end_line: int
    change_type: str                   # added | modified | deleted

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ChangedSymbol":
        return ChangedSymbol(
            name=d["name"],
            kind=d.get("kind", "other"),
            path=d["path"],
            language=d.get("language", ""),
            start_line=int(d.get("start_line", 0)),
            end_line=int(d.get("end_line", 0)),
            change_type=d.get("change_type", CHANGE_MODIFIED),
        )


# ---------------------------------------------------------------------------
# Blast-radius result: the context bundle.
# ---------------------------------------------------------------------------
@dataclass
class ImpactItem:
    """One symbol that may be affected by a change, with why we think so."""
    symbol: Symbol
    reason: str                        # edge_type or "semantic"
    depth: int                         # graph distance from a changed symbol (0 = semantic-only)
    confidence: float
    cross_service: bool                # lives in a different top-level service/dir
    score: float                       # fused rank score (higher = more relevant)
    via: list[str] = field(default_factory=list)  # names of changed symbols it traces to

    def to_dict(self) -> dict:
        d = asdict(self)
        d["symbol"] = self.symbol.to_row() | {"path": self.symbol.path}
        return d


@dataclass
class ContextBundle:
    """The full answer to 'what can this change break?' — handed to the agent."""
    changed: list[ChangedSymbol]
    impacted: list[ImpactItem]
    tests: list[ImpactItem]            # subset flagged as likely-relevant tests
    truncated: bool = False            # True if the impact set was capped

    def to_dict(self) -> dict:
        return {
            "changed": [c.to_dict() for c in self.changed],
            "impacted": [i.to_dict() for i in self.impacted],
            "tests": [t.to_dict() for t in self.tests],
            "truncated": self.truncated,
        }


@dataclass
class IndexResult:
    """Everything produced by a full index pass over a repo."""
    repo_path: str
    commit_sha: Optional[str]
    symbols: list[Symbol]
    edges: list[Edge]
    chunks: list[Chunk]

    def stats(self) -> dict:
        return {
            "symbols": len(self.symbols),
            "edges": len(self.edges),
            "chunks": len(self.chunks),
            "embedded": sum(1 for c in self.chunks if c.embedding is not None),
        }
