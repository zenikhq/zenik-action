"""Language detection, the target-language set, and grammar prefetch.

Grammars in tree-sitter-language-pack are downloaded on demand and cached, not
bundled. In a long-lived dev/server process that is fine (first touch downloads,
rest hit the cache), but in CI it means a network round-trip mid-index unless the
cache is warmed first. `prefetch()` warms it deliberately; the action wires it as
a setup step so indexing never blocks on a download.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional

import tree_sitter_language_pack as tlp

# Directories we never index: build output, dependencies, VCS metadata. Mirrors
# the agent's "do not edit" list in the prototype prompt — the same noise.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build",
    "out", "target", ".venv", "venv", "__pycache__", ".next", ".turbo",
    ".mypy_cache", ".pytest_cache", "coverage", ".idea", ".vscode",
    "site-packages", "bower_components", ".gradle", "Pods",
}

# The languages v0 targets. Detection can return others (the pack knows 300+);
# these are the ones we actively index. Kept broad enough to cover the 8+-language
# test repo without committing to a per-language resolver for any of them.
TARGET_LANGUAGES = {
    "python", "typescript", "tsx", "javascript", "jsx", "go", "ruby",
    "java", "csharp", "rust", "cpp", "c", "php", "kotlin", "scala", "swift",
}

# Files above this size are almost always generated/minified; parsing them wastes
# time and pollutes the index. 1 MiB is generous for hand-written source.
MAX_FILE_BYTES = 1_048_576


def detect_language(path: str) -> Optional[str]:
    """Return the tree-sitter language name for a path, or None if unsupported.

    Uses the pack's own extension map, then filters to our target set so a stray
    `.json`/`.md` never becomes a symbol source.
    """
    try:
        lang = tlp.detect_language_from_path(path)
    except Exception:
        lang = None
    if lang and lang in TARGET_LANGUAGES:
        return lang
    return None


def iter_source_files(root: str) -> Iterator[tuple[str, str]]:
    """Yield (absolute_path, language) for every indexable source file under root.

    Prunes SKIP_DIRS in-place so we never descend into node_modules et al., and
    skips oversized/binary files.
    """
    root_path = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune skipped directories from the walk (mutating dirnames is the
        # documented way to stop os.walk descending).
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            lang = detect_language(full)
            if not lang:
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield full, lang


def prefetch(languages: Optional[list[str]] = None) -> list[str]:
    """Warm the grammar cache for the given languages (default: TARGET_LANGUAGES).

    Best-effort: a language that fails to download is skipped, not fatal — the
    file that needed it simply goes unindexed rather than failing the whole run.
    Returns the list actually available afterwards.
    """
    langs = languages or sorted(TARGET_LANGUAGES)
    ok: list[str] = []
    for lang in langs:
        try:
            tlp.get_parser(lang)  # forces download + load into the cache
            ok.append(lang)
        except Exception as e:  # noqa: BLE001 - one bad grammar must not abort prefetch
            print(f"[languages] prefetch: could not load '{lang}': {type(e).__name__}")
    return ok
