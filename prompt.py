#!/usr/bin/env python3
"""Builds the FINDINGS prompt handed to the coding agent.

This is a rewrite of the prototype's fix prompt. The prototype asked the agent
to EDIT code to migrate a vendor API change. Zenik v0 asks the agent to do
something deliberately narrower and safer: **explain the blast radius of a PR in
prose, and change nothing.**

Two inputs feed the prompt:

  1. The PR diff (what the developer actually changed) — from git.
  2. The context bundle from the platform's `/v1/impact` — the fused
     blast-radius set: the impacted call sites / services / tests, each with a
     reason (a provable import/call edge, or a semantic look-alike), a
     confidence, and a cross-service flag.

The agent's job is the v0 output spec (BUILD.md §5): for each affected site,
say **what** is affected and **why it is risky**, in prose — level 2 guidance.
Not level 3 (actual code diffs). The prompt therefore spends real length
telling the agent NOT to edit files: it is running unattended with file-write
tools, and the single worst outcome in v0 is a "findings" run that quietly
rewrites the client's code.

The blast-radius set is a CANDIDATE list, fused for recall. The agent is asked
to read the real code at each site and prune false positives — an item the
embedding search surfaced that is not actually affected should be dropped, not
dressed up with a risk it doesn't have.
"""
from __future__ import annotations

# Cap how much of the raw diff we inline, so a huge PR cannot blow the agent's
# context window. The agent has read access to the whole checkout, so the diff
# is a pointer, not the only source of truth.
_MAX_DIFF_CHARS = 12000


def _fmt_changed(changed: list[dict]) -> str:
    if not changed:
        return "- (no changed symbols were resolved from the diff)"
    lines = []
    for c in changed:
        loc = f"{c.get('path')}:{c.get('start_line')}"
        lines.append(
            f"- `{c.get('name')}` ({c.get('kind')}, {c.get('language')}) "
            f"at {loc} — {c.get('change_type')}"
        )
    return "\n".join(lines)


def _fmt_impacted(impacted: list[dict]) -> str:
    if not impacted:
        return "- (the blast-radius query returned no impacted sites)"
    lines = []
    for it in impacted:
        s = it.get("symbol", {})
        loc = f"{s.get('path')}:{s.get('start_line')}"
        flag = " [CROSS-SERVICE]" if it.get("cross_service") else ""
        via = it.get("via") or []
        via_txt = f" via {', '.join(via)}" if via else ""
        lines.append(
            f"- `{s.get('name')}` at {loc}{flag} — reason: {it.get('reason')}, "
            f"depth {it.get('depth')}, confidence {it.get('confidence')}"
            f"{via_txt}"
        )
    return "\n".join(lines)


def _fmt_tests(tests: list[dict]) -> str:
    if not tests:
        return "- (no likely-relevant tests were flagged)"
    lines = []
    for it in tests:
        s = it.get("symbol", {})
        loc = f"{s.get('path')}:{s.get('start_line')}"
        lines.append(f"- `{s.get('name')}` at {loc} — {it.get('reason')}")
    return "\n".join(lines)


def build_findings_prompt(diff_text: str, bundle: dict) -> str:
    """Assemble the findings prompt from a PR diff and the impact bundle.

    `bundle` is the platform's `/v1/impact` response — the ContextBundle.to_dict()
    shape (`changed` / `impacted` / `tests` / `truncated`).
    """
    changed = bundle.get("changed") or []
    impacted = bundle.get("impacted") or []
    tests = bundle.get("tests") or []
    truncated = bundle.get("truncated")

    diff_blob = (diff_text or "").strip()
    if len(diff_blob) > _MAX_DIFF_CHARS:
        diff_blob = (diff_blob[:_MAX_DIFF_CHARS]
                     + "\n... (diff truncated; read the files directly for the rest)")
    if not diff_blob:
        diff_blob = "(no textual diff available; work from the changed symbols and the checkout)"

    truncated_note = (
        "\n\nNOTE: the impact set was CAPPED — there may be more affected sites "
        "than are listed. Say so in your summary." if truncated else ""
    )

    return f"""You are running inside a CI job on a pull request. Your job is to write a
clear, human-readable **change-impact report** for the developer who opened this
PR. You are NOT here to fix anything — you produce PROSE ONLY.

Zenik has already computed the blast radius of this change: the list of code
that may be affected, fused from two signals — provable import/call edges, and
semantic look-alikes (code that does a similar thing, possibly in another
language or service, that no import connects). Your task is to turn that
candidate list into guidance a reviewer can act on.

## What the PR changed (diff)

```diff
{diff_blob}
```

## Changed symbols (resolved from the diff)

{_fmt_changed(changed)}

## Blast radius — candidate impacted sites (from Zenik's index)

Each item below is a CANDIDATE. `reason: imports` / `references` / `calls_maybe`
/ `tested_by` are deterministic edges (someone literally depends on the changed
code). `reason: semantic` means the embedding search found look-alike code — it
may be a real duplicate of the logic, or it may be a false positive. Items
marked [CROSS-SERVICE] live in a different top-level service/dir — those are the
"easy for a human reviewer to miss" cases and are the most valuable to get right.

{_fmt_impacted(impacted)}

## Likely-relevant tests

{_fmt_tests(tests)}
{truncated_note}

## Your task — findings + per-caller guidance (prose, no code changes)

Produce a concise report with these parts:

1. **One-line summary** — what changed and how wide the blast radius is
   (e.g. "changes `charge()`; affects N call sites across M services").

2. **Per-site guidance** — for EACH impacted site you judge to be genuinely
   affected, one short paragraph or bullet stating:
   - *what* is affected (which caller / service / test), and
   - *why it is risky* — trace it to the specific change. Be concrete:
     "this caller still passes the old 2-arg signature and will need the new
     `currency` argument", not "this may be affected".
   Read the actual code at the site (you have the full checkout) to confirm the
   risk before you assert it. Flag cross-service sites explicitly — those are
   the wins.

3. **Pruned candidates** — list any candidate sites you concluded are NOT
   actually affected (especially `semantic` look-alikes that just resemble the
   changed code), with one clause each on why you dropped them. Pruning false
   positives is as valuable as finding real ones.

4. **Tests to run** — the likely-relevant tests a reviewer should run before
   merging.

## Hard rules — read these

- **Do NOT edit, create, move, or delete any file.** Do not produce diffs,
  patches, or code suggestions. This is a findings-only run. If you catch
  yourself about to write to a file, stop — the output is your text report and
  nothing else.
- **Do NOT run commands that mutate the repo** (no `git commit`, no formatters,
  no code generators). Reading files is fine and encouraged.
- Ground every claim in the code. If you are unsure whether a site is affected,
  say so plainly rather than inventing a risk or hiding the doubt.
- Keep it factual and concise — this text becomes a PR comment a busy reviewer
  reads in thirty seconds. No preamble, no restating these instructions.

Reply with ONLY the report."""
