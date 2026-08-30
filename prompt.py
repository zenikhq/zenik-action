#!/usr/bin/env python3
"""Build the FINDINGS prompt from (PR diff + blast-radius context bundle).

Rewritten from the prototype's `build_fix_prompt`, which built a *fix* prompt
from a vendor change. The shape is deliberately kept close to it so that turning
this back into a fix prompt later is an added section, not a rewrite
(BUILD.md §9.1).

**Level 2, not level 3.** The agent explains what is affected and why it is
risky, in prose. It must not write code diffs. That boundary is stated three
times in the prompt below — once as a rule, once in the output format, and once
as a negative example — because a coding agent's default instinct is to fix
things, and a single mention does not reliably hold it back.
"""

from __future__ import annotations

MAX_DIFF_CHARS = 60_000
MAX_SITES = 25


def _format_site(item: dict, index: int) -> str:
    bits = [f"{index}. {item['path']}:{item.get('start_line') or '?'} — `{item['name']}`"]
    meta = []
    if item.get("kind"):
        meta.append(item["kind"])
    if item.get("language"):
        meta.append(item["language"])
    if item.get("depth") is not None:
        hops = "direct caller" if item["depth"] == 1 else f"{item['depth']} hops away"
        meta.append(hops)
    if item.get("via"):
        meta.append("via " + " → ".join(item["via"]))
    if item.get("sources") == ["vector"]:
        meta.append("NO import/reference edge — related semantically only")
    if item.get("cross_language"):
        meta.append("CROSS-LANGUAGE / likely cross-service")
    if meta:
        bits.append("   (" + "; ".join(meta) + ")")
    return "\n".join(bits)


def build_findings_prompt(
    *,
    diff: str,
    changed_symbols: list[dict],
    bundle: dict,
    repo_full_name: str,
    pr_number: int | None,
) -> str:
    impact = (bundle.get("impact") or [])[:MAX_SITES]
    tests = bundle.get("tests") or []

    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated ...]"

    changed_lines = "\n".join(
        f"  - `{c['name']}` ({c.get('kind', '?')}) in {c['path']}:"
        f"{c.get('start_line', '?')}-{c.get('end_line', '?')}"
        for c in changed_symbols
    ) or "  (none identified)"

    sites = "\n".join(_format_site(i, n) for n, i in enumerate(impact, 1)) or "  (none)"
    test_lines = "\n".join(f"  - {t['path']} (`{t['name']}`)" for t in tests) or "  (none)"

    return f"""You are reviewing a pull request in `{repo_full_name}`\
{f" (PR #{pr_number})" if pr_number else ""}.

Zenik has already computed the **blast radius** of this change — the set of code
elsewhere in this repository that may be affected. Your job is to explain, for a
developer reading the PR, what that means in practice.

## The change

Symbols modified in this PR:
{changed_lines}

Diff:
```diff
{diff}
```

## Blast radius (computed, not guessed)

These sites were selected by fusing two independent signals: a deterministic
walk over import/reference edges, and semantic similarity over the indexed
codebase. Sites marked "NO import/reference edge" were found *only* semantically
— they are the likeliest to be a cross-service connection (an HTTP route, a
queue, an RPC boundary) and also the likeliest to be a false positive. Judge
them on the evidence in the diff.

{sites}

Tests that reference the changed code:
{test_lines}

## What to produce

For each affected site that you judge to be **genuinely at risk**, write a short
prose note covering:
  - **what** at that site depends on the changed code, and
  - **why this specific change puts it at risk** — reference the actual edit
    (a renamed parameter, a changed return shape, a removed field, new
    behaviour), not a generic caution.

Rules:
  - **Do NOT write code, patches, diffs, or suggested edits.** Prose only.
    The developer decides what to change; you are telling them what to look at.
  - **Drop sites you believe are unaffected.** A short accurate list beats a
    long hedged one. Say so explicitly if you are dropping most of them.
  - If a site is affected but you cannot tell how without seeing more of it,
    say that plainly rather than guessing.
  - Never invent a call site that is not in the list above.
  - Do not modify any file in this repository. This is a read-only review.

## Output format

Write GitHub-flavoured Markdown, nothing else:

### <path>:<line> — `<symbol>`
<one or two sentences: what depends on the change, and the specific risk.>

Then, at the end:

### Summary
<one or two sentences: overall risk of this PR, and what most deserves a human's
attention.>

A correct answer looks like:

    ### workers/retry_job.go:31 — `RetryCharge`
    Calls `charge()` across the service boundary and still reads the `retries`
    field this PR removed from the response, so it will fail at runtime rather
    than at build time.

NOT like:

    ### workers/retry_job.go:31 — `RetryCharge`
    ```go
    - retries := resp.retries
    + retries := resp.Attempts
    ```

The second form is a code fix. Do not produce it.
"""
