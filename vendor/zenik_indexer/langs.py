"""Language detection and source-file discovery.

One uniform API over 371 grammars via tree-sitter-language-pack — no
per-language resolvers anywhere in this package (BUILD.md §2).

NOTE on `tree-sitter-language-pack`: grammars are fetched on demand and cached,
not bundled in the wheel. In CI that means a cold runner would hit the network
mid-index, so `prefetch_languages()` exists to pull them up front. BUILD.md §3
recommends `tree-sitter-languages`, which is unmaintained and does not build on
Python 3.12 — this package is its maintained successor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import tree_sitter_language_pack as tlp

# Directories that never carry first-party source. Skipping them is the single
# biggest factor in index size and cost.
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "venv", ".venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "dist", "build", "target", "out", "bin", "obj", ".next", ".nuxt",
    ".gradle", ".idea", ".vscode", "coverage", ".terraform", "Pods",
    "site-packages", ".zenik-cache", ".eggs",
}

IGNORED_SUFFIXES = {
    ".min.js", ".min.css", ".map", ".lock", ".snap", ".pb.go", "_pb2.py",
    ".generated.ts", ".g.dart", ".designer.cs",
}

# Markup/config grammars parse fine but carry no callable symbols; indexing them
# adds noise to the vector layer without adding impact signal.
NON_CODE_LANGUAGES = {
    "json", "yaml", "toml", "markdown", "html", "css", "scss", "xml",
    "dockerfile", "gitignore", "csv", "ini", "text", "vimdoc", "requirements",
    "properties", "env", "editorconfig", "svg", "graphql", "proto_text",
}

MAX_FILE_BYTES = 800_000


def detect(path: str | Path) -> str | None:
    lang = tlp.detect_language_from_path(str(path))
    if lang and lang in NON_CODE_LANGUAGES:
        return None
    return lang


def is_indexable(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return False
    if any(name.endswith(sfx) for sfx in IGNORED_SUFFIXES):
        return False
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    return detect(path) is not None


def iter_source_files(root: str | Path) -> Iterator[Path]:
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if is_indexable(path):
            yield path


def prefetch_languages(languages: list[str]) -> dict[str, bool]:
    """Warm the grammar cache so an index run makes no network calls."""
    out: dict[str, bool] = {}
    for lang in sorted(set(languages)):
        try:
            tlp.get_parser(lang)
            out[lang] = True
        except Exception:
            out[lang] = False
    return out
