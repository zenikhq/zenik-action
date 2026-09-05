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


def _name_status(repo: str, base: Optional[str], head: Optional[str]
                 ) -> dict[str, tuple[str, Optional[str]]]:
    """path -> (single-letter status A/M/D/R/C..., old path for renames/copies).

    Uses rename detection; for R/C rows the key is the NEW path and the second
    element is the OLD path (None for plain A/M/D rows)."""
    code, out = _git(repo, ["diff", "--name-status", "--find-renames", *_diff_args(base, head)])
    status: dict[str, tuple[str, Optional[str]]] = {}
    if code != 0:
        return status
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        letter = parts[0][0]
        # For renames/copies (R100/C75) the row is "R100\told\tnew".
        path = parts[-1]
        old_path = parts[1] if len(parts) >= 3 else None
        status[path] = (letter, old_path)
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


# Symbol kinds that are CONTAINERS of definitions: a hunk inside one of their
# members is a change to the member, not to the container. Everything else
# (function, method, variable, ...) is a callable/value in its own right, and a
# change inside something it encloses changes it too.
_CONTAINER_KINDS = frozenset({"class", "interface", "enum", "type", "module", "namespace"})


def _innermost_hits(syms: list[Symbol], rngs: list[tuple[int, int]]) -> list[Symbol]:
    """Symbols the changed ranges touch, attributed to the innermost definition
    — but only past CONTAINER kinds.

    Symbols nest (a method inside a class, an inner function inside a
    function). A hunk inside `OrderRoutingService.release` is a change to
    `release`; emitting the class too would seed the walk with every user of
    the class and drown the real blast radius. An enclosing container is still
    emitted when some changed line falls in its own body outside every nested
    hit (a field edit, a class-level annotation), so nothing goes invisible.

    An enclosing FUNCTION or METHOD is always emitted: a decorator's inner
    `wrapper`, a closure, a nested helper are part of the outer function's
    behaviour, and the outer name is usually the only one callers reference
    (`wrapper` is defined in every decorator module and resolves to nothing).
    """
    hits = [s for s in syms if _intersects(s, rngs)]
    out: list[Symbol] = []
    for s in hits:
        if s.kind not in _CONTAINER_KINDS:
            out.append(s)
            continue
        inner = [
            t for t in hits
            if t is not s
            and t.start_line >= s.start_line and t.end_line <= s.end_line
            and (t.end_line - t.start_line) < (s.end_line - s.start_line)
        ]
        if not inner:
            out.append(s)
            continue
        own_line_changed = any(
            not any(t.start_line <= line <= t.end_line for t in inner)
            for a, b in rngs
            for line in range(max(a, s.start_line), min(b, s.end_line) + 1)
        )
        if own_line_changed:
            out.append(s)
    return out


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
        _emit_changed(cs)

    def _emit_changed(cs: ChangedSymbol):
        k = f"{cs.path}::{cs.name}::{cs.start_line}::{cs.change_type}"
        if k not in seen:
            seen.add(k)
            out.append(cs)

    def _warn_no_base(path: str):
        print(f"[diff] WARNING: cannot read base version of {path} at {base_ref} — "
              "its deletions/renames are invisible to blast radius (shallow clone? "
              "fetch the base commit in CI)")

    for path, (letter, old_path) in status.items():
        lang = languages.detect_language(path)
        if not lang:
            continue

        if letter in ("R", "C") and old_path:
            # File rename (or copy) — the stored graph only knows the OLD path,
            # so seeds must come from the base version at the old path. Every
            # symbol in a renamed file "moves": importers of the old module
            # path are all affected, so emit them all, module symbol included.
            base_src = _content_at_ref(repo, base_ref, old_path)
            if base_src is None:
                _warn_no_base(old_path)
            else:
                for s in _symbols_of(old_path, base_src):
                    _emit(s, CHANGE_MODIFIED)
                fb = _module_fallback(old_path, base_src, CHANGE_MODIFIED)
                if fb:
                    _emit_changed(fb)
            # Content edits on top of the rename: match the new side as usual.
            rng = ranges.get(path, {"new": [], "old": []})
            head_src = _content_head(repo, head, path)
            head_syms = _symbols_of(path, head_src) if head_src is not None else []
            for s in _innermost_hits(head_syms, rng["new"]):
                _emit(s, CHANGE_MODIFIED)
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
            if src is None:
                _warn_no_base(path)
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
        for s in _innermost_hits(head_syms, rng["new"]):
            _emit(s, CHANGE_MODIFIED)
            matched = True

        base_src = _content_at_ref(repo, base_ref, path)
        if base_src is None:
            _warn_no_base(path)
        base_syms = _symbols_of(path, base_src) if base_src is not None else []
        for s in _innermost_hits(base_syms, rng["old"]):
            _emit(s, CHANGE_MODIFIED)
            matched = True

        if not matched:
            src = head_src if head_src is not None else base_src
            if src is not None:
                fb = _module_fallback(path, src, CHANGE_MODIFIED)
                if fb:
                    out.append(fb)

    return out
