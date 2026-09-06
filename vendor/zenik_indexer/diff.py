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


def _innermost_hits(syms: list[Symbol], rngs: list[tuple[int, int]],
                    source: Optional[bytes] = None) -> list[Symbol]:
    """Symbols the changed ranges touch, attributed to the innermost definition
    — but only past CONTAINER kinds.

    Symbols nest (a method inside a class, an inner function inside a
    function). A hunk inside `OrderRoutingService.release` is a change to
    `release`; emitting the class too would seed the walk with every user of
    the class and drown the real blast radius. An enclosing container is still
    emitted when some changed line falls in its own body outside every nested
    definition (a field edit, a class-level annotation), so nothing goes
    invisible.

    An enclosing FUNCTION or METHOD is always emitted: a decorator's inner
    `wrapper`, a closure, a nested helper are part of the outer function's
    behaviour, and the outer name is usually the only one callers reference
    (`wrapper` is defined in every decorator module and resolves to nothing).

    Lines that precede a definition with no blank line in between — a javadoc,
    a `///` block, a Go doc comment, an annotation/attribute — belong to that
    definition, not to the container whose body they happen to sit in. That is
    purely positional (the extractor's spans start at the declaration, never at
    its comment), so it holds for every language alike. A comment separated
    from the next definition by a blank line is nobody's doc and still falls to
    the container, as does a field or any other container-level line. Blank
    lines are ignored unless they are the only thing a range touched.
    """
    lines: list[str] = source.decode("utf-8", "replace").splitlines() if source else []

    def _blank(line: int) -> bool:
        return 1 <= line <= len(lines) and not lines[line - 1].strip()

    def _contains(sym: Symbol, line: int) -> bool:
        return sym.start_line <= line <= sym.end_line

    starts: dict[int, list[Symbol]] = {}
    for s in syms:
        starts.setdefault(s.start_line, []).append(s)

    def _documented_by(line: int) -> list[Symbol]:
        """The definitions this line is a contiguous preamble of, if any."""
        enclosing = [s for s in syms if _contains(s, line)]
        cur = line
        while cur <= len(lines) and not _blank(cur):
            cur += 1
            hit = starts.get(cur)
            if hit:
                # Everything enclosing the comment must also enclose the
                # definition, or the comment sits outside its scope.
                return [t for t in hit if all(_contains(e, t.start_line) for e in enclosing)]
        return []

    def _attribute(line: int, blank_ok: bool) -> list[Symbol]:
        if _blank(line) and not blank_ok:
            return []
        enclosing = [s for s in syms if _contains(s, line)]
        hit = [s for s in enclosing if s.kind not in _CONTAINER_KINDS]
        if hit:
            return hit
        doc = _documented_by(line) if not _blank(line) else []
        if doc:
            # The doc'd definition, plus any non-container it nests inside
            # (an inner function's comment is still a change to the outer).
            return doc + [s for s in enclosing if s.kind not in _CONTAINER_KINDS]
        containers = [s for s in enclosing if s.kind in _CONTAINER_KINDS]
        if not containers:
            return []
        return [min(containers, key=lambda s: s.end_line - s.start_line)]

    changed_lines = sorted({line for a, b in rngs for line in range(a, b + 1)})
    hits: list[Symbol] = []
    for blank_ok in (False, True):
        for line in changed_lines:
            for s in _attribute(line, blank_ok):
                if s not in hits:
                    hits.append(s)
        if hits:
            break
    hits.sort(key=lambda s: (s.start_line, -(s.end_line - s.start_line)))
    return hits


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
            for s in _innermost_hits(head_syms, rng["new"], head_src):
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

        # Modified: intersect changed lines with symbols on both sides. The
        # head side catches edits and additions; the base side catches
        # removals. One symbol usually shows up on both — with different line
        # numbers whenever anything above it moved — so the two sides are
        # merged by (name, kind): the head-side span wins (it is what the
        # inline comment anchors to), and a base-only symbol is a deletion.
        rng = ranges.get(path, {"new": [], "old": []})

        head_src = _content_head(repo, head, path)
        head_syms = _symbols_of(path, head_src) if head_src is not None else []
        head_hits = _innermost_hits(head_syms, rng["new"], head_src)

        base_src = _content_at_ref(repo, base_ref, path)
        if base_src is None:
            _warn_no_base(path)
        base_syms = _symbols_of(path, base_src) if base_src is not None else []
        base_hits = _innermost_hits(base_syms, rng["old"], base_src)

        merged: list[tuple[Symbol, str]] = [(s, CHANGE_MODIFIED) for s in head_hits]
        for s in base_hits:
            same = [t for t in head_syms if t.name == s.name and t.kind == s.kind]
            if same:
                # Survives on head (an edit that only removed lines): take the
                # head span, nearest one when the name is overloaded.
                t = min(same, key=lambda t: abs(t.start_line - s.start_line))
                if all(t is not h for h, _ in merged):
                    merged.append((t, CHANGE_MODIFIED))
            else:
                merged.append((s, CHANGE_DELETED))

        for s, change_type in merged:
            _emit(s, change_type)

        if not merged:
            src = head_src if head_src is not None else base_src
            if src is not None:
                fb = _module_fallback(path, src, CHANGE_MODIFIED)
                if fb:
                    out.append(fb)

    return out
