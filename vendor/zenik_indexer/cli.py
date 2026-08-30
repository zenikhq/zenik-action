"""zenik-indexer command line.

    zenik-indexer index <path> [--out index.json] [--no-embed] [--offline]
    zenik-indexer impact <path> (--diff | --changed NAME...) [--index index.json]
                                [--base REF] [--head REF] [--depth N]
                                [--no-semantic] [--offline] [--json]
    zenik-indexer diff-symbols <path> [--base REF] [--head REF]
    zenik-indexer prefetch [langs...]

`index` and `impact` run fully standalone — no Supabase, no CI. `diff-symbols` is
the entry `zenik-action` calls to turn a PR diff into the changed-symbol payload.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import languages
from .diff import changed_symbols as compute_changed
from .embeddings import get_embedder
from .impact import ImpactEngine
from .indexer import build_index
from .model import ChangedSymbol, ContextBundle, IndexResult
from .store import load_index, save_index


def _load_env_file() -> None:
    """Best-effort: if OPENAI_API_KEY isn't already set, pull it (and friends)
    from a .env.zenik found by walking up from cwd. Keeps local runs turnkey
    without a python-dotenv dependency. Real env vars always win."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    here = Path.cwd()
    for base in [here, *here.parents]:
        env = base / ".env.zenik"
        if env.exists():
            try:
                for line in env.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            except OSError:
                pass
            return


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_index(args) -> int:
    embedder = get_embedder(prefer_offline=args.offline)
    result = build_index(
        args.path, embedder=embedder, embed=not args.no_embed,
    )
    out = args.out or os.path.join(args.path, ".zenik-index.json")
    save_index(result, out)
    print(f"[index] wrote {out}  {result.stats()}")
    return 0


def _changed_from_names(index: IndexResult, names: list[str]) -> list[ChangedSymbol]:
    wanted = set(names)
    out: list[ChangedSymbol] = []
    for s in index.symbols:
        if s.name in wanted:
            out.append(ChangedSymbol(
                name=s.name, kind=s.kind, path=s.path, language=s.language,
                start_line=s.start_line, end_line=s.end_line,
                change_type="modified",
            ))
    missing = wanted - {c.name for c in out}
    for m in missing:
        print(f"[impact] warning: no symbol named '{m}' in the index", file=sys.stderr)
    return out


def cmd_impact(args) -> int:
    # Obtain the index (load or build fresh).
    if args.index:
        index = load_index(args.index)
    else:
        embedder = get_embedder(prefer_offline=args.offline)
        index = build_index(
            args.path, embedder=embedder, embed=not args.no_semantic,
            progress=not args.json,
        )

    # Determine the changed symbols.
    if args.diff:
        changed = compute_changed(args.path, base=args.base, head=args.head)
    elif args.changed:
        changed = _changed_from_names(index, args.changed)
    else:
        print("[impact] specify --diff or --changed NAME...", file=sys.stderr)
        return 2

    if not changed:
        print("[impact] no changed symbols resolved; nothing to do")
        if args.json:
            print(json.dumps({"changed": [], "impacted": [], "tests": []}))
        return 0

    bundle = ImpactEngine(index).compute(
        changed, max_depth=args.depth, semantic=not args.no_semantic,
    )

    if args.json:
        print(json.dumps(bundle.to_dict(), indent=2))
    else:
        _print_bundle(bundle)
    return 0


def cmd_diff_symbols(args) -> int:
    changed = compute_changed(args.path, base=args.base, head=args.head)
    print(json.dumps([c.to_dict() for c in changed], indent=2))
    return 0


def cmd_prefetch(args) -> int:
    ok = languages.prefetch(args.langs or None)
    print(f"[prefetch] ready: {', '.join(ok)}")
    return 0


# ---------------------------------------------------------------------------
# human-readable impact rendering (mirrors the v0 PR-comment shape)
# ---------------------------------------------------------------------------
def _print_bundle(bundle: ContextBundle) -> None:
    print("\nZenik — change impact")
    print("Changed symbols:")
    for c in bundle.changed:
        print(f"  · {c.name}  ({c.path}:{c.start_line}) [{c.change_type}]")

    print(f"\nImpacted: {len(bundle.impacted)} site(s)"
          + ("  (truncated)" if bundle.truncated else ""))
    for it in bundle.impacted:
        s = it.symbol
        flag = "  ⚠ cross-service" if it.cross_service else ""
        print(f"  • {s.path}:{s.start_line}  {s.name} "
              f"— {it.reason} (conf {it.confidence}, score {it.score}){flag}")

    if bundle.tests:
        print(f"\nTests likely relevant: {len(bundle.tests)}")
        for it in bundle.tests:
            s = it.symbol
            print(f"  • {s.path}:{s.start_line}  {s.name} — {it.reason}")
    print()


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zenik-indexer", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="build and persist an index")
    pi.add_argument("path")
    pi.add_argument("--out", default=None, help="output JSON (default <path>/.zenik-index.json)")
    pi.add_argument("--no-embed", action="store_true", help="skip embeddings (symbols/edges only)")
    pi.add_argument("--offline", action="store_true", help="force offline hash embedder")
    pi.set_defaults(func=cmd_index)

    pm = sub.add_parser("impact", help="compute blast radius for a change")
    pm.add_argument("path")
    pm.add_argument("--diff", action="store_true", help="derive changed symbols from git diff")
    pm.add_argument("--changed", nargs="+", metavar="NAME", help="symbol name(s) treated as changed")
    pm.add_argument("--index", default=None, help="load a prebuilt index JSON instead of reindexing")
    pm.add_argument("--base", default=None, help="git base ref for --diff")
    pm.add_argument("--head", default=None, help="git head ref for --diff")
    pm.add_argument("--depth", type=int, default=3, help="max reverse-walk depth")
    pm.add_argument("--no-semantic", action="store_true", help="deterministic edges only")
    pm.add_argument("--offline", action="store_true", help="force offline hash embedder")
    pm.add_argument("--json", action="store_true", help="emit the context bundle as JSON")
    pm.set_defaults(func=cmd_impact)

    pd = sub.add_parser("diff-symbols", help="git diff -> changed-symbols JSON (action entry)")
    pd.add_argument("path")
    pd.add_argument("--base", default=None)
    pd.add_argument("--head", default=None)
    pd.set_defaults(func=cmd_diff_symbols)

    pp = sub.add_parser("prefetch", help="warm the grammar cache")
    pp.add_argument("langs", nargs="*", help="languages (default: all targets)")
    pp.set_defaults(func=cmd_prefetch)

    return p


def main(argv: list[str] | None = None) -> int:
    _load_env_file()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
