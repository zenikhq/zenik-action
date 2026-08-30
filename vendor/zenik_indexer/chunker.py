"""Syntax-aware chunking (BUILD.md §2 Layer A).

Chunks follow symbol boundaries — a whole function/method/class — never fixed
line windows. Two refinements matter:

  * A class whose body contains its own extracted functions is skipped in favour
    of those functions, so the same lines are not embedded twice.
  * Lines belonging to no symbol (imports, top-level config, module docstrings)
    still get chunked under the file's module symbol. That is often where a
    cross-service connection lives — a base URL, a queue name, a route string —
    and dropping it would blind the semantic layer to exactly the cross-service
    edges §7.2 warns about.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .extract import FileExtract, Symbol

MAX_CHUNK_CHARS = 6000
MIN_CHUNK_CHARS = 24


@dataclass
class Chunk:
    path: str
    start_line: int
    end_line: int
    language: str
    symbol_name: str | None
    text: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _slice(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start - 1 : end])


def _split_oversized(chunk: Chunk) -> list[Chunk]:
    if len(chunk.text) <= MAX_CHUNK_CHARS:
        return [chunk]
    lines = chunk.text.split("\n")
    per = max(1, len(lines) * MAX_CHUNK_CHARS // max(1, len(chunk.text)))
    out: list[Chunk] = []
    for i in range(0, len(lines), per):
        window = lines[i : i + per]
        if not window:
            continue
        out.append(
            Chunk(
                path=chunk.path,
                start_line=chunk.start_line + i,
                end_line=chunk.start_line + i + len(window) - 1,
                language=chunk.language,
                symbol_name=chunk.symbol_name,
                text="\n".join(window),
            )
        )
    return out


def chunk_file(fx: FileExtract) -> list[Chunk]:
    if fx.error or not fx.source:
        return []
    try:
        lines = fx.source.decode("utf-8").split("\n")
    except UnicodeDecodeError:
        return []

    # A container symbol is one that wholly contains another extracted symbol.
    # Prefer the inner ones; embedding both duplicates the same lines.
    leaves: list[Symbol] = []
    for s in fx.symbols:
        contains_other = any(
            o is not s and s.start_line <= o.start_line and s.end_line >= o.end_line
            for o in fx.symbols
        )
        if not contains_other:
            leaves.append(s)

    chunks: list[Chunk] = []
    covered: set[int] = set()
    for s in sorted(leaves, key=lambda s: s.start_line):
        text = _slice(lines, s.start_line, s.end_line)
        if len(text.strip()) < MIN_CHUNK_CHARS:
            continue
        covered.update(range(s.start_line, s.end_line + 1))
        chunks.extend(
            _split_oversized(
                Chunk(fx.path, s.start_line, s.end_line, fx.language, s.name, text)
            )
        )

    # Remaining lines -> module-level chunks, split at gaps.
    module_name = fx.module_symbol.name
    run: list[int] = []
    for lineno in range(1, len(lines) + 1):
        if lineno in covered or not lines[lineno - 1].strip():
            if run:
                text = _slice(lines, run[0], run[-1])
                if len(text.strip()) >= MIN_CHUNK_CHARS:
                    chunks.extend(
                        _split_oversized(
                            Chunk(fx.path, run[0], run[-1], fx.language, module_name, text)
                        )
                    )
                run = []
            continue
        run.append(lineno)
    if run:
        text = _slice(lines, run[0], run[-1])
        if len(text.strip()) >= MIN_CHUNK_CHARS:
            chunks.extend(
                _split_oversized(
                    Chunk(fx.path, run[0], run[-1], fx.language, module_name, text)
                )
            )

    return chunks
