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


def build_findings_prompt(diff_text: str, bundle: dict,
                          intent: str = "") -> str:
    """Assemble the findings prompt from a PR diff and the impact bundle.

    `bundle` is the platform's `/v1/impact` response — the ContextBundle.to_dict()
    shape (`changed` / `impacted` / `tests` / `truncated`). `intent` is the
    author's PR title + description (Zenik's own block already stripped) —
    what the author SAYS the change does, for the mismatch check.
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

    intent_blob = (intent or "").strip()
    if len(intent_blob) > 4000:
        intent_blob = intent_blob[:4000] + "\n... (description truncated)"
    intent_section = (
        f"""

## What the author SAYS this PR does (title + description)

{intent_blob}

Treat this as the author's stated INTENT, not as fact — the code is the fact.
""" if intent_blob else ""
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
```{intent_section}

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

Your findings are split across the PR: a short note is pinned INLINE on each
changed symbol's diff hunk, and one compact summary comment covers the rest.
So keep every piece brief — the inline note is what a reviewer reads first.

Produce a concise report with these parts:

1. **One-line summary** — what changed and how wide the blast radius is
   (e.g. "changes `charge()`; affects N call sites across M services").

2. **Per-site guidance** — for EACH impacted site you judge to be genuinely
   affected, one short bullet stating:
   - *what* is affected (which caller / service / test), and
   - *why it is risky* — trace it to the specific change. Be concrete:
     "this caller still passes the old 2-arg signature and will need the new
     `currency` argument", not "this may be affected".
   Read the actual code at the site (you have the full checkout) to confirm the
   risk before you assert it. Flag cross-service sites explicitly — those are
   the wins.

3. **Pruned candidates** — list any candidate sites you concluded are NOT
   actually affected, with one clause each on why you dropped them. Pruning
   false positives is as valuable as finding real ones. `semantic` look-alikes
   deserve one extra distinction: code that INDEPENDENTLY implements the same
   logic as the changed code (a parallel implementation that should be kept in
   sync by hand) is NOT pruned noise — put it in `parallel` in the JSON block
   below instead.

4. **Tests to run** — the likely-relevant tests a reviewer should run before
   merging.

5. **Intent check** — only if the author's stated intent was provided above:
   compare what the description SAYS against what the diff DOES. If the code
   clearly does something more, less, or different than stated (touches a
   system the description never mentions, silently changes behaviour beyond
   the stated scope, or doesn't do the stated thing), say so in one sentence
   in `intent_mismatch`. A wording gap is not a mismatch — leave it empty
   unless a reviewer trusting the description would be misled.

## Machine-readable block — REQUIRED, last thing in your reply

End your reply with exactly one fenced ```json block (nothing after it):

```json
{{
  "per_symbol": [
    {{"name": "<changed symbol name>", "note": "<1-3 plain sentences: the real risk for this symbol's callers. Simple technical English. State the problem directly; no hedging, no jargon padding.>"}}
  ],
  "parallel": [
    {{"path": "<file>", "line": <line>, "why": "<one clause: what logic it duplicates>"}}
  ],
  "overall": "<1-2 sentences for the summary comment>",
  "intent_mismatch": "<one sentence ONLY if the code clearly diverges from the author's stated intent; otherwise an empty string>"
}}
```

One `per_symbol` entry per changed symbol (use the exact symbol names listed
above). If a symbol's change is harmless, say so in its note in a few words.
`parallel` may be empty. The notes are posted verbatim as inline PR comments —
write them for the developer, not for a log.

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


def _fmt_findings(findings: list[str]) -> str:
    if not findings:
        return ("(no prior Zenik findings comments were found on the PR — "
                "work from the blast radius above)")
    joined = "\n\n---\n\n".join(f.strip() for f in findings)
    if len(joined) > 8000:
        joined = joined[:8000] + "\n... (findings truncated)"
    return joined


def build_fix_prompt(diff_text: str, bundle: dict, findings: list[str],
                     scope: list[str]) -> str:
    """The `/zenik fix` prompt — the inverse of the findings prompt: editing is
    now the JOB, but only what the findings implicate, as minimally as possible.
    The orchestrator additionally enforces the file scope in code (out-of-scope
    edits are reverted), so the prompt and the harness agree."""
    changed = bundle.get("changed") or []
    impacted = bundle.get("impacted") or []
    tests = bundle.get("tests") or []

    diff_blob = (diff_text or "").strip()
    if len(diff_blob) > _MAX_DIFF_CHARS:
        diff_blob = (diff_blob[:_MAX_DIFF_CHARS]
                     + "\n... (diff truncated; read the files directly for the rest)")

    scope_note = (
        f"\n\nThe requester scoped this run to: {', '.join(f'`{s}`' for s in scope)}."
        " Only act on findings about those changed symbols." if scope else ""
    )

    return f"""You are running inside a CI job on a pull request, invoked by a maintainer
who commented `/zenik fix`. A previous Zenik run reported this change's blast
radius and per-caller guidance. Your job now is to **apply those fixes to the
affected code** — edit files, carefully and minimally.{scope_note}

## What the PR changed (diff)

```diff
{diff_blob}
```

## Changed symbols

{_fmt_changed(changed)}

## Blast radius — the sites the fixes are FOR

{_fmt_impacted(impacted)}

## Likely-relevant tests

{_fmt_tests(tests)}

## Zenik's prior findings on this PR (what the maintainer is asking you to apply)

{_fmt_findings(findings)}

## Your task

For each genuinely affected site in the blast radius, apply the fix the
findings describe (or the fix the code plainly needs once you read it):
update callers to the changed behaviour, keep parallel logic consistent where
a finding says so, and update the flagged tests if they now assert stale
behaviour. Read the real code at every site before editing it.

## Hard rules — read these

- **Edit ONLY files named in the blast radius above** (the changed files, the
  impacted sites, the flagged tests). The harness reverts anything else, so
  out-of-scope edits are wasted work. Never touch `.github/`.
- **Minimal diffs.** No reformatting, no renames, no refactors, no new
  dependencies, no drive-by cleanups. A reviewer must be able to map every
  hunk you produce to a finding.
- Do NOT run `git commit`/`git push` — the harness commits for you. Running
  the repo's tests to check your work is fine and encouraged.
- If a finding is wrong or a fix would be risky, SKIP it and say why in your
  summary rather than forcing a bad edit.

End your reply with a short summary for the PR thread: what you changed per
file, what you skipped and why. Plain technical English, no preamble."""
