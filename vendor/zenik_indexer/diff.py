"""PR diff -> changed symbols.

This is the entry point `zenik-action` calls: given a git range (or the working
tree), return the list of `ChangedSymbol`s a diff actually touched, by mapping
changed line ranges onto symbol spans via the same tree-sitter extraction the
full index uses. Consistency matters — a "changed symbol" here must be the same
shape and boundaries as the symbols the index stores, or the platform can't line
them up.

Modes:
  * base and head refs given   -> committed range  (the CI/PR case)
  * base ref only              -> base .. working tree
  * neither                    -> working tree vs HEAD (uncommitted edits)
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from . import languages
from .extract import extract_file
from .model import (
    ChangedSymbol, Symbol,
    CHANGE_ADDED, CHANGE_MODIFIED, CHANGE_DELETED,
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _git(repo: str, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo,
            capture_output=True, text=True, timeout=120,
        )
        return proc.returncode, proc.stdout
    except Exception as e:  # noqa: BLE001
        print(f"[diff] git {' '.join(args)} failed: {type(e).__name__}")
        return 1, ""


def _diff_args(base: Optional[str], head: Optional[str]) -> list[str]:
    if base and head:
        return [f"{base}", f"{head}"]
    if base and not head:
        return [f"{base}"]
    return ["HEAD"]


def _name_status(repo: str, base: Optional[str], head: Optional[str]) -> dict[str, str]:
    """path -> single-letter status (A/M/D/R/C...) using rename detection."""
    code, out = _git(repo, ["diff", "--name-status", "--find-renames", *_diff_args(base, head)])
    status: dict[str, str] = {}
    if code != 0:
        return status
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        letter = parts[0][0]
        # For renames/copies (R100/C75) the NEW path is the last field.
        path = parts[-1]
        status[path] = letter
    return status


def _changed_ranges(repo: str, base: Optional[str], head: Optional[str]
                    ) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """path -> {'new': [(start,end)...], 'old': [(start,end)...]} of changed lines."""
    code, out = _git(repo, ["diff", "--unified=0", "--find-renames", *_diff_args(base, head)])
    ranges: dict[str, dict[str, list[tuple[int, int]]]] = {}
    cur: Optional[str] = None
    if code != 0:
        return ranges
    for line in out.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            cur = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            if cur:
                ranges.setdefault(cur, {"new": [], "old": []})
            continue
        m = _HUNK_RE.match(line)
        if m and cur:
            old_start, old_count, new_start, new_count = m.groups()
            oc = 1 if old_count is None else int(old_count)
            nc = 1 if new_count is None else int(new_count)
            os_, ns_ = int(old_start), int(new_start)
            if nc > 0:
                ranges[cur]["new"].append((ns_, ns_ + nc - 1))
            if oc > 0:
                ranges[cur]["old"].append((os_, os_ + oc - 1))
    return ranges


def _content_at_ref(repo: str, ref: str, path: str) -> Optional[bytes]:
    try:
        proc = subprocess.run(
            ["git", "show", f"{ref}:{path}"], cwd=repo,
            capture_output=True, timeout=60,
        )
        return proc.stdout if proc.returncode == 0 else None
    except Exception:
        return None


def _content_head(repo: str, head: Optional[str], path: str) -> Optional[bytes]:
    if head:
        return _content_at_ref(repo, head, path)
    full = Path(repo) / path
    try:
        return full.read_bytes()
    except OSError:
        return None


def _symbols_of(rel_path: str, source: bytes) -> list[Symbol]:
    lang = languages.detect_language(rel_path)
    if not lang or source is None:
        return []
    fx = extract_file(rel_path, source, lang)
    return fx.symbols if fx else []


def _intersects(sym: Symbol, rngs: list[tuple[int, int]]) -> bool:
    return any(not (sym.end_line < s or sym.start_line > e) for s, e in rngs)


def _module_fallback(rel_path: str, source: bytes, change_type: str) -> Optional[ChangedSymbol]:
    """When a diff touches a file but no symbol span covers the changed lines
    (top-level code, config-ish edits), fall back to the file's module symbol so
    the change is never invisible to blast radius."""
    lang = languages.detect_language(rel_path)
    if not lang or source is None:
        return None
    fx = extract_file(rel_path, source, lang)
    if not fx:
        return None
    m = fx.module_symbol
    return ChangedSymbol(
        name=m.name, kind=m.kind, path=m.path, language=m.language,
        start_line=m.start_line, end_line=m.end_line, change_type=change_type,
    )


def changed_symbols(
    repo_path: str,
    base: Optional[str] = None,
    head: Optional[str] = None,
) -> list[ChangedSymbol]:
    """Compute the changed symbols for a git range (see module docstring)."""
    repo = str(Path(repo_path).resolve())
    base_ref = base or "HEAD"
    status = _name_status(repo, base, head)
    ranges = _changed_ranges(repo, base, head)

    out: list[ChangedSymbol] = []
    seen: set[str] = set()

    def _emit(sym: Symbol, change_type: str):
        cs = ChangedSymbol(
            name=sym.name, kind=sym.kind, path=sym.path, language=sym.language,
            start_line=sym.start_line, end_line=sym.end_line, change_type=change_type,
        )
        k = f"{cs.path}::{cs.name}::{cs.start_line}::{cs.change_type}"
        if k not in seen:
            seen.add(k)
            out.append(cs)

    for path, letter in status.items():
        lang = languages.detect_language(path)
        if not lang:
            continue

        if letter == "A":
            src = _content_head(repo, head, path)
            syms = _symbols_of(path, src) if src is not None else []
            if syms:
                for s in syms:
                    _emit(s, CHANGE_ADDED)
            elif src is not None:
                fb = _module_fallback(path, src, CHANGE_ADDED)
                if fb:
                    out.append(fb)
            continue

        if letter == "D":
            src = _content_at_ref(repo, base_ref, path)
            syms = _symbols_of(path, src) if src is not None else []
            for s in syms:
                _emit(s, CHANGE_DELETED)
            continue

        # Modified / renamed / copied: intersect changed lines with symbols on
        # both sides (new side catches edits+additions, old side catches removals).
        rng = ranges.get(path, {"new": [], "old": []})
        matched = False

        head_src = _content_head(repo, head, path)
        head_syms = _symbols_of(path, head_src) if head_src is not None else []
        for s in head_syms:
            if _intersects(s, rng["new"]):
                _emit(s, CHANGE_MODIFIED)
                matched = True

        base_src = _content_at_ref(repo, base_ref, path)
        base_syms = _symbols_of(path, base_src) if base_src is not None else []
        for s in base_syms:
            if _intersects(s, rng["old"]):
                _emit(s, CHANGE_MODIFIED)
                matched = True

        if not matched:
            src = head_src if head_src is not None else base_src
            if src is not None:
                fb = _module_fallback(path, src, CHANGE_MODIFIED)
                if fb:
                    out.append(fb)

    return out
