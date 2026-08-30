"""PR diff -> changed symbols (BUILD.md §3, §5).

Takes the hunks git reports, then asks tree-sitter which symbol spans those
lines fall inside. A hunk touching no symbol (an import block, a constant) maps
to the file's module symbol, so a change is never silently un-attributed.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .extract import extract_file

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ChangedSymbol:
    name: str
    path: str
    kind: str
    language: str
    start_line: int
    end_line: int


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def changed_line_ranges(root: Path, base: str, head: str = "HEAD") -> dict[str, list[tuple[int, int]]]:
    """{path: [(start, end), ...]} for lines present in `head` and not `base`."""
    merge_base = _git(root, "merge-base", base, head).strip() or base
    raw = _git(root, "diff", "--unified=0", "--no-color", f"{merge_base}..{head}")

    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            ranges.setdefault(current, [])
        elif line.startswith("+++ /dev/null"):
            current = None
        elif line.startswith("@@") and current:
            m = HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or 1)
                if count > 0:
                    ranges[current].append((start, start + count - 1))
    return {p: r for p, r in ranges.items() if r}


def changed_symbols(
    root: Path, base: str, head: str = "HEAD"
) -> tuple[list[ChangedSymbol], list[str]]:
    """Returns (changed symbols, the changed source snippets).

    The snippets are what gets embedded for the semantic half of the query.
    They are embedded client-side and never sent anywhere as text.
    """
    out: list[ChangedSymbol] = []
    snippets: list[str] = []

    for rel_path, hunks in changed_line_ranges(root, base, head).items():
        abs_path = root / rel_path
        if not abs_path.exists():
            continue
        fx = extract_file(abs_path, rel_path)
        if fx is None or fx.error:
            continue

        lines = fx.source.decode("utf-8", "replace").split("\n")
        matched_any = False
        for sym in fx.symbols:
            if any(not (h_end < sym.start_line or h_start > sym.end_line)
                   for h_start, h_end in hunks):
                matched_any = True
                out.append(
                    ChangedSymbol(
                        name=sym.name, path=rel_path, kind=sym.kind,
                        language=sym.language, start_line=sym.start_line,
                        end_line=sym.end_line,
                    )
                )
                snippets.append("\n".join(lines[sym.start_line - 1 : sym.end_line]))

        if not matched_any:
            mod = fx.module_symbol
            out.append(
                ChangedSymbol(
                    name=mod.name, path=rel_path, kind="module",
                    language=mod.language, start_line=mod.start_line,
                    end_line=mod.end_line,
                )
            )
            for h_start, h_end in hunks:
                snippets.append("\n".join(lines[h_start - 1 : h_end]))

    # An edited method reports both the method and its enclosing class; the
    # class adds nothing to the query and drags in the whole file's context.
    innermost: list[ChangedSymbol] = []
    for c in out:
        encloses_another = any(
            o is not c and o.path == c.path
            and c.start_line <= o.start_line and c.end_line >= o.end_line
            for o in out
        )
        if not encloses_another:
            innermost.append(c)
    return innermost, [s for s in snippets if s.strip()]
