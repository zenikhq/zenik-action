"""zenik-indexer — Zenik's code-intelligence engine.

Public surface (stable; the action and platform import these):

  * build_index(repo_path)                 -> IndexResult      (indexer.py)
  * changed_symbols(repo_path, base, head)  -> [ChangedSymbol]  (diff.py)
  * compute_impact(index, changed)          -> ContextBundle    (impact.py)

Data contracts live in model.py; the CLI in cli.py.
"""
from .diff import changed_symbols
from .impact import ImpactEngine, compute_impact
from .indexer import build_index
from .model import (
    ChangedSymbol, Chunk, ContextBundle, Edge, ImpactItem, IndexResult, Symbol,
)

__all__ = [
    "build_index",
    "changed_symbols",
    "compute_impact",
    "ImpactEngine",
    "ChangedSymbol",
    "Chunk",
    "ContextBundle",
    "Edge",
    "ImpactItem",
    "IndexResult",
    "Symbol",
]

__version__ = "0.0.1"
