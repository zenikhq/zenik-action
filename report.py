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
via a comment posted as `zenik-ai[bot]` (the Zenik GitHub App). The trust footer
is factual: the whole thing ran on their runner with their keys, and only
counts left the building.

Also here: the **check run** payload (`build_check_run`) — the "Zenik" check
on the PR head, with one annotation per changed symbol that has callers. It
never blocks a merge on findings: `neutral` when there are findings, `success`
when there are none, `failure` only when Zenik itself broke.
"""
from __future__ import annotations

import json
import re

# Keep the inline blast-radius list readable in a PR comment; the full set is
# always in the dashboard / the job log.
_MAX_LISTED = 25

# Callers listed inside one inline comment's <details>; the rest go to the
# dashboard. Inline comments must stay short or they defeat their purpose.
_MAX_INLINE_CALLERS = 10

# The check run on the PR head. GitHub accepts at most 50 annotations per
# check-run request; beyond that the rest is in the comments/dashboard.
CHECK_RUN_NAME = "Zenik"
_MAX_ANNOTATIONS = 50
_MAX_ANNOTATION_CALLERS = 5


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

# Same idea for the inline review comments: review comments can't be PATCHed the
# way issue comments can, so re-runs find comments carrying this marker, delete
# them, and post a fresh review.
INLINE_MARKER = "<!-- zenik-inline -->"

# The PR-description block. Industry lesson (CodeRabbit appends into the body,
# Qodo /describe rewrites it wholesale — both get complaints): NEVER touch the
# author's text. Zenik owns exactly the region between these two markers at the
# BOTTOM of the description, replaces only that region on re-runs, and writes
# nothing at all when the client opts out (`update-pr-description: "false"`).
DESC_MARKER_START = "<!-- zenik:impact -->"
DESC_MARKER_END = "<!-- /zenik:impact -->"

_DESC_BLOCK_RE = re.compile(
    re.escape(DESC_MARKER_START) + r".*?" + re.escape(DESC_MARKER_END),
    re.DOTALL,
)


def strip_description_block(body) -> str:
    """The author's own description text: everything outside Zenik's marker
    region. Used both when refreshing the block and when feeding the body to
    the agent as intent (our own counts must not read back as author intent)."""
    return _DESC_BLOCK_RE.sub("", body or "").rstrip()


def build_description_block(bundle: dict, structured=None) -> str:
    """The 3-4 line block appended to the PR description. Counts only — the
    detail lives in the inline comments and the summary comment."""
    c = _counts(bundle)
    if c["impacted"]:
        cross = f", {c['cross_service']} cross-service ⚠" if c["cross_service"] else ""
        tests = f" · {c['tests']} test(s) to run" if c["tests"] else ""
        headline = (f"🛰 **Zenik:** {c['changed']} changed symbol(s) → "
                    f"{c['impacted']} affected site(s){cross}{tests} · "
                    "details in the Zenik comments below")
    else:
        headline = "🛰 **Zenik:** no impacted callers found for this change"
    lines = [DESC_MARKER_START, "", "---", headline]
    mismatch = ((structured or {}).get("intent_mismatch") or "").strip()
    if mismatch:
        lines.append(f"> ⚠️ **Description vs code:** {mismatch}")
    lines += ["", DESC_MARKER_END]
    return "\n".join(lines)


def merge_description(existing_body, block: str) -> str:
    """Author text verbatim + exactly one Zenik block at the bottom."""
    author = strip_description_block(existing_body)
    return (author + "\n\n" + block + "\n").lstrip("\n")

_JSON_FENCE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def parse_agent_message(text):
    """Split the agent's reply into (prose, structured-dict-or-None).

    The prompt asks for a trailing ```json block with per-symbol notes. Parse
    the LAST such block leniently; on any failure return the full text as prose
    and None — the report then degrades to the single-comment layout.
    """
    if not (text or "").strip():
        return "", None
    matches = list(_JSON_FENCE.finditer(text))
    for m in reversed(matches):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("per_symbol"), list):
            prose = (text[:m.start()] + text[m.end():]).strip()
            return prose, data
    return text.strip(), None


def note_for(structured, name: str):
    for entry in (structured or {}).get("per_symbol") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            note = (entry.get("note") or "").strip()
            return note or None
    return None


def callers_of(bundle: dict, name: str) -> list[dict]:
    """The impacted items that trace back to changed symbol `name` (via)."""
    out = []
    for it in bundle.get("impacted") or []:
        if name in (it.get("via") or []):
            out.append(it)
    return out


def build_inline_body(changed: dict, callers: list[dict], note) -> str:
    """One inline review comment, pinned on the changed symbol's diff hunk.

    Kept deliberately brief: headline counts, the agent's note (1-3 plain
    sentences), and the caller list folded away. The reviewer should get the
    point without scrolling.
    """
    name = changed.get("name")
    cross = sum(1 for c in callers if c.get("cross_service"))
    head = f"**Zenik** — `{name}`: **{len(callers)}** caller(s)"
    if cross:
        head += f", {cross} cross-service ⚠"
    head += "."

    lines = [INLINE_MARKER, head]
    if note:
        lines += ["", note]
    if callers:
        lines += ["", "<details><summary>Callers</summary>", ""]
        for it in callers[:_MAX_INLINE_CALLERS]:
            s = it.get("symbol") or {}
            flag = " ⚠" if it.get("cross_service") else ""
            lines.append(f"- `{s.get('path')}:{s.get('start_line')}` — "
                         f"`{s.get('name')}` ({it.get('reason')}){flag}")
        if len(callers) > _MAX_INLINE_CALLERS:
            lines.append(f"- … {len(callers) - _MAX_INLINE_CALLERS} more "
                         f"(see the Zenik dashboard)")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def _parallel_section(structured) -> list[str]:
    """The third category: code that independently implements the same logic.
    Not blast radius, not noise — a keep-in-sync reminder."""
    items = [p for p in (structured or {}).get("parallel") or []
             if isinstance(p, dict) and p.get("path")]
    if not items:
        return []
    out = ["", "### Parallel implementations — keep in sync", "",
           "_Not affected by this change, but they implement the same logic "
           "independently. If the rule changed, change them too._", ""]
    for p in items[:_MAX_LISTED]:
        loc = p.get("path")
        if p.get("line"):
            loc = f"{loc}:{p['line']}"
        why = (p.get("why") or "").strip()
        out.append(f"- `{loc}`" + (f" — {why}" if why else ""))
    return out


def build_report(*, bundle: dict, agent_result, outcome: str,
                 pr_number=None, truncated: bool = False,
                 agent_prose=None, structured=None,
                 inline_posted: bool = False) -> str:
    """Return the Markdown body for the summary PR comment.

    With `inline_posted` the per-symbol detail lives in inline review comments
    on the diff, so this stays a short overview. Without it (review post failed,
    or nothing was anchorable) this carries the full detail as before.
    `agent_prose` is the agent's reply with the machine JSON block stripped.
    """
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

    mismatch = ((structured or {}).get("intent_mismatch") or "").strip()
    if mismatch:
        lines += [
            f"> ⚠️ **The PR description and the code disagree:** {mismatch}",
            "",
        ]

    if inline_posted:
        # Per-symbol detail is pinned inline on the diff; keep this an overview.
        lines += ["_Per-symbol findings are pinned inline on the changed code._",
                  ""]
        overall = ((structured or {}).get("overall") or "").strip()
        prose = overall or (agent_prose or "").strip()
        if prose:
            lines += ["### Findings", "", prose]
        lines += _tests_list(bundle)
    else:
        lines += ["### Blast radius", ""]
        lines += _impact_list(bundle)
        lines += _tests_list(bundle)

        # The agent's prose findings — the level-2 guidance.
        prose = (agent_prose if agent_prose is not None
                 else (agent_result.final_message or "")
                 if agent_result is not None else "").strip()
        if agent_result is not None and prose:
            lines += ["", "### Findings & guidance", "", prose]
        elif agent_result is not None and agent_result.ok:
            lines += [
                "",
                "### Findings & guidance",
                "",
                "_The agent completed but returned no summary text. The blast "
                "radius above is the index's own answer._",
            ]

    lines += _parallel_section(structured)

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

    # The opt-in fix affordance — only when there is something to fix.
    if c["impacted"] and outcome != "agent_failed":
        lines += [
            "",
            "> 💡 To have Zenik apply these fixes to this branch, comment "
            "`/zenik fix` — or `/zenik fix <symbol>` to scope it. "
            "Requires write access; you review the diff.",
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
    """A one-line summary — the check run's title."""
    c = _counts(bundle)
    if outcome == "agent_failed":
        return "Zenik: agent failed; blast radius still posted"
    if c["impacted"] == 0:
        return "Zenik: no impact found for this change"
    extra = f", {c['cross_service']} cross-service" if c["cross_service"] else ""
    return f"Zenik: {c['impacted']} affected site(s){extra}"


def check_conclusion(bundle: dict, outcome: str) -> str:
    """`success` with no findings, `neutral` with findings, `failure` only
    when Zenik itself errored. Findings never block a merge."""
    if outcome == "agent_failed":
        return "failure"
    return "neutral" if _counts(bundle)["impacted"] else "success"


def build_check_annotations(bundle: dict) -> list[dict]:
    """One annotation per changed symbol that has callers, on the symbol's
    span in the PR head. Deleted symbols have no lines in the head file, so
    they are skipped (their detail is in the comments). Capped at GitHub's
    50-per-request limit."""
    out: list[dict] = []
    for ch in bundle.get("changed") or []:
        if ch.get("change_type") == "deleted":
            continue
        path = ch.get("path")
        start = int(ch.get("start_line") or 0)
        end = int(ch.get("end_line") or 0)
        if not path or start < 1:
            continue
        callers = callers_of(bundle, ch.get("name"))
        if not callers:
            continue
        names = [(c.get("symbol") or {}).get("name") or "?" for c in callers]
        listed = ", ".join(names[:_MAX_ANNOTATION_CALLERS])
        if len(callers) > _MAX_ANNOTATION_CALLERS:
            listed += f", … {len(callers) - _MAX_ANNOTATION_CALLERS} more"
        cross = any(c.get("cross_service") for c in callers)
        out.append({
            "path": path,
            "start_line": start,
            "end_line": max(end, start),
            "annotation_level": "warning" if cross else "notice",
            "title": f"Zenik: {ch.get('name')}",
            "message": (f"`{ch.get('name')}` affects {len(callers)} caller(s)"
                        + (" (cross-service)" if cross else "")
                        + f": {listed}"),
        })
        if len(out) >= _MAX_ANNOTATIONS:
            break
    return out


def build_check_run(bundle: dict, outcome: str, head_sha: str) -> dict:
    """The completed check-run body for POST/PATCH /check-runs."""
    c = _counts(bundle)
    summary = (f"**{c['changed']}** changed symbol(s) → **{c['impacted']}** "
               f"potentially affected site(s)")
    if c["cross_service"]:
        summary += f", **{c['cross_service']}** cross-service ⚠"
    summary += "."
    if c["tests"]:
        summary += f" {c['tests']} test(s) likely relevant."
    if outcome == "agent_failed":
        summary += " The findings agent did not complete — see the job log."
    summary += "\n\nDetails are in the Zenik comments on this PR."
    return {
        "name": CHECK_RUN_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": check_conclusion(bundle, outcome),
        "output": {
            "title": check_summary(bundle, outcome),
            "summary": summary,
            "annotations": build_check_annotations(bundle),
        },
    }
