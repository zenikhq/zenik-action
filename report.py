#!/usr/bin/env python3
"""Writes the findings summary that becomes the PR comment body.

Rewritten from the prototype's `fix_report.md` writer. The prototype described
an automated FIX (a PR full of edits). Zenik v0 describes a change's blast
radius in prose — no edits — so this builds a comment with two layers:

  1. A deterministic **blast-radius list** straight from the platform's impact
     bundle (path:line, why it's implicated, whether it crosses a service
     boundary). This is shown even if the agent never ran, because it is the
     index's own answer and does not depend on the LLM.

  2. The agent's **prose findings + per-caller guidance** — the level-2 output
     (BUILD.md §5). Passed through as written; the agent's own words about what
     is risky and what it pruned are the most useful part.

Audience: the client's own engineer reading the PR their teammate just opened,
via a comment authored by their own `github-actions[bot]`. The trust footer is
factual: the whole thing ran on their runner with their keys, and only counts
left the building.
"""
from __future__ import annotations

# Keep the inline blast-radius list readable in a PR comment; the full set is
# always in the dashboard / the job log.
_MAX_LISTED = 25


def _counts(bundle: dict) -> dict:
    changed = bundle.get("changed") or []
    impacted = bundle.get("impacted") or []
    tests = bundle.get("tests") or []
    cross = [it for it in impacted if it.get("cross_service")]
    services = set()
    for it in impacted:
        path = (it.get("symbol") or {}).get("path") or ""
        top = path.split("/", 1)[0] if "/" in path else path
        if top:
            services.add(top)
    return {
        "changed": len(changed),
        "impacted": len(impacted),
        "tests": len(tests),
        "cross_service": len(cross),
        "services": len(services),
    }


def _agent_line(agent_result) -> str:
    if agent_result is None:
        return "_no agent configured — showing the index's blast radius only._"
    bits = [f"`{agent_result.backend}`"]
    if agent_result.model:
        bits.append(f"model `{agent_result.model}`")
    if agent_result.cli_version:
        bits.append(f"CLI `{agent_result.cli_version}`")
    return ", ".join(bits)


def _usage_line(agent_result):
    if agent_result is None:
        return None
    u = agent_result.usage
    parts = []
    if u.input_tokens is not None:
        parts.append(f"{u.input_tokens:,} in")
    if u.output_tokens is not None:
        parts.append(f"{u.output_tokens:,} out")
    if u.cached_input_tokens:
        parts.append(f"{u.cached_input_tokens:,} cached")
    if u.cost_usd is not None:
        parts.append(f"~${u.cost_usd:.4f}")
    return " / ".join(parts) if parts else None


def _impact_list(bundle: dict) -> list[str]:
    impacted = bundle.get("impacted") or []
    if not impacted:
        return ["_No affected sites were found for this change._"]
    lines = []
    for it in impacted[:_MAX_LISTED]:
        s = it.get("symbol") or {}
        loc = f"{s.get('path')}:{s.get('start_line')}"
        flag = " &nbsp;⚠ **cross-service**" if it.get("cross_service") else ""
        lines.append(f"- `{loc}` — `{s.get('name')}` ({it.get('reason')}){flag}")
    if len(impacted) > _MAX_LISTED:
        lines.append(f"- … {len(impacted) - _MAX_LISTED} more (see the Zenik dashboard)")
    return lines


def _tests_list(bundle: dict) -> list[str]:
    tests = bundle.get("tests") or []
    if not tests:
        return []
    out = ["", "**Tests likely relevant:**", ""]
    for it in tests[:_MAX_LISTED]:
        s = it.get("symbol") or {}
        out.append(f"- `{s.get('path')}:{s.get('start_line')}` — `{s.get('name')}`")
    return out


# Marker so re-runs on the same PR can find and update the prior comment instead
# of stacking duplicates. Kept as an HTML comment so it is invisible in render.
COMMENT_MARKER = "<!-- zenik-action:change-impact -->"


def build_report(*, bundle: dict, agent_result, outcome: str,
                 pr_number=None, truncated: bool = False) -> str:
    """Return the Markdown body for the PR comment."""
    c = _counts(bundle)

    summary = (
        f"**{c['changed']}** changed symbol(s) → **{c['impacted']}** potentially "
        f"affected site(s)"
    )
    if c["services"]:
        summary += f" across **{c['services']}** service area(s)"
    if c["cross_service"]:
        summary += f", **{c['cross_service']}** cross-service ⚠"
    summary += "."

    lines = [
        COMMENT_MARKER,
        "## Zenik — change impact",
        "",
        summary,
        "",
    ]

    if truncated or bundle.get("truncated"):
        lines += [
            "> ⚠ The impact set was capped — there may be more affected sites "
            "than shown.",
            "",
        ]

    lines += ["### Blast radius", ""]
    lines += _impact_list(bundle)
    lines += _tests_list(bundle)

    # The agent's prose findings — the level-2 guidance.
    if agent_result is not None and agent_result.final_message:
        lines += [
            "",
            "### Findings & guidance",
            "",
            agent_result.final_message.strip(),
        ]
    elif agent_result is not None and agent_result.ok:
        lines += [
            "",
            "### Findings & guidance",
            "",
            "_The agent completed but returned no summary text. The blast radius "
            "above is the index's own answer._",
        ]

    if outcome == "agent_failed":
        err = (agent_result.error if agent_result else "unknown error")[:1500]
        lines += [
            "",
            "### ⚠️ The agent did not complete",
            "",
            f"```\n{err}\n```",
            "",
            "The blast-radius list above still stands (it comes from Zenik's "
            "index, not the agent), but the prose guidance is missing for this run.",
        ]

    # Footer: engine, cost, and the trust note.
    lines += ["", "---", ""]
    lines.append(f"**Analysis by:** {_agent_line(agent_result)}")
    usage = _usage_line(agent_result)
    if usage:
        lines.append(f"**Token usage:** {usage} _(billed to this repo's own key)_")
    lines += [
        "",
        "_Ran entirely on this repository's own CI runner. Your source never left "
        "your environment — only a derived index (symbols, edges, embeddings; no "
        "source text) reached Zenik, and telemetry is counts-only (printed in full "
        "in this job's log)._",
    ]

    return "\n".join(lines) + "\n"


def check_summary(bundle: dict, outcome: str) -> str:
    """A one-line summary for the commit Check / status."""
    c = _counts(bundle)
    if outcome == "agent_failed":
        return "Zenik: agent failed; blast radius still posted"
    if c["impacted"] == 0:
        return "Zenik: no impact found for this change"
    extra = f", {c['cross_service']} cross-service" if c["cross_service"] else ""
    return f"Zenik: {c['impacted']} affected site(s){extra}"
