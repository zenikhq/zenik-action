#!/usr/bin/env python3
"""Render the findings into the PR comment body.

Repurposed from the prototype's `write_report`, which wrote a fix report. The
structure — a deterministic header we control, then the agent's prose — is kept
because it means a garbled or empty agent response still produces a comment that
tells the developer what Zenik found, rather than nothing.
"""

from __future__ import annotations

from pathlib import Path

# Lets the action find and update its own previous comment instead of posting a
# new one on every push to the PR.
MARKER = "<!-- zenik-blast-radius -->"


def _site_line(item: dict) -> str:
    where = f"{item['path']}:{item.get('start_line') or '?'}"
    notes = []
    if item.get("depth") == 1:
        notes.append("direct caller")
    elif item.get("depth") is not None:
        notes.append(f"{item['depth']} hops")
    if item.get("sources") == ["vector"]:
        notes.append("semantic only")
    if item.get("cross_language"):
        notes.append("⚠ cross-service")
    suffix = f" — {', '.join(notes)}" if notes else ""
    return f"| `{where}` | `{item['name']}` | {item.get('language') or '?'} | {suffix.lstrip(' —')} |"


def build_comment(
    *,
    changed_symbols: list[dict],
    bundle: dict,
    agent_markdown: str,
    outcome: str,
    stats: dict,
    run_url: str | None = None,
) -> str:
    impact = bundle.get("impact") or []
    tests = bundle.get("tests") or []

    changed = ", ".join(f"`{c['name']}`" for c in changed_symbols[:8]) or "—"
    if len(changed_symbols) > 8:
        changed += f" and {len(changed_symbols) - 8} more"

    lines = [
        MARKER,
        "## Zenik — change impact",
        "",
        f"Changed: {changed}",
        "",
    ]

    if not impact:
        lines += [
            "No downstream code appears to depend on what this PR changes.",
            "",
        ]
    else:
        cross = sum(1 for i in impact if i.get("cross_language"))
        headline = f"**{len(impact)} affected site(s)**"
        if cross:
            headline += f", {cross} crossing a language/service boundary"
        lines += [
            headline,
            "",
            "| Site | Symbol | Lang | Why |",
            "|---|---|---|---|",
            *[_site_line(i) for i in impact[:25]],
            "",
        ]
        if len(impact) > 25:
            lines.append(f"_…and {len(impact) - 25} more._\n")

    if tests:
        # One test file usually yields several symbol rows (its module symbol
        # plus each test function); the developer wants the file listed once.
        seen: set[str] = set()
        test_paths = [
            t["path"] for t in tests
            if not (t["path"] in seen or seen.add(t["path"]))
        ]
        lines += [
            "**Tests likely relevant:** "
            + ", ".join(f"`{p}`" for p in test_paths[:8]),
            "",
        ]

    if agent_markdown.strip():
        lines += ["---", "", agent_markdown.strip(), ""]
    else:
        lines += [
            "---",
            "",
            "_The review agent did not return guidance for this run; the impact "
            "list above is still computed and complete._",
            "",
        ]

    footer = (
        f"<sub>outcome: `{outcome}` · "
        f"edge candidates: {stats.get('edge_candidates', '?')} · "
        f"semantic candidates: {stats.get('vector_candidates', '?')}"
    )
    if run_url:
        footer += f" · [run]({run_url})"
    footer += " · guidance only, no code changes were made</sub>"
    lines.append(footer)

    return "\n".join(lines)


def write_report(body: str, path: Path) -> None:
    """Also leave the comment on disk, so a run without a PR still produces
    something a developer can read (and so the step is debuggable locally)."""
    try:
        path.write_text(body, encoding="utf-8")
        print(f"[report] wrote {path}")
    except OSError as exc:
        print(f"[report] could not write {path}: {exc}")
