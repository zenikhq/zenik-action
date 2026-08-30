# zenik-action

The client-side GitHub Action. Runs in the **client's own CI** on
`on: pull_request`, computes the blast radius of the change, and posts findings
back on the PR.

## What runs where

| | Where | Credential |
|---|---|---|
| Diff → changed symbols | client's runner | — |
| Embedding the changed hunks | client's runner | client's `OPENAI_API_KEY` |
| Blast-radius query | zenik-platform | client's `ZENIK_CLIENT_KEY` |
| Review agent | client's runner, sandboxed | client's agent key |
| PR comment + check | client's repo | client's `GITHUB_TOKEN` |

**Source code never leaves the runner.** The diff is embedded locally; only
vectors reach the platform. Zenik holds no GitHub credential for a client repo
and has no GitHub identity — the comment is authored by the client's own
`github-actions[bot]` (BUILD.md §10.1).

## Reused from the prior prototype (BUILD.md §6)

- `agent_backends.py` — **verbatim**. Codex + Claude Code adapters, pinned CLI
  versions, API key scoped to the subprocess, sandbox-failure detection.
- `telemetry.py` — same closed-allowlist discipline and salted `hash_repo`;
  payload adapted (vendor-change fields dropped, agent/usage flattened to the
  platform's `telemetry` columns).
- `action.yml` — same composite structure and the Codex/bubblewrap AppArmor prep
  step, which is a genuinely hard-won fix for Ubuntu 24.04 runners.
- `apply_fix.py` → `zenik_pr.py` — same main() flow, with the platform
  round-trip inserted before the agent call.
- `legacy_scan.py` — **dropped**, as instructed.

## v0 is guidance-only, and that is enforced

The prompt forbids code edits three separate times, and `enforce_read_only()`
reverts the checkout if the agent writes anything anyway. The PR comment claims
no code was changed; this makes that claim true rather than hopeful.

## Two things to decide before this ships to a real client

1. **`zenik-action` must be readable by the client's runner for `uses:` to
   resolve.** SETUP.md creates all four repos private, and a private repo in
   `zenikhq` cannot be consumed by a repo outside the org. This one probably has
   to be public (it contains no secrets — the IP is in the indexer and the
   platform). Not decided in BUILD.md.
2. **The semantic layer needs an embedding key on the runner.** A client using
   Claude Code has `ANTHROPIC_API_KEY` but no OpenAI key, so the run silently
   degrades to edges-only. Options: accept it, ask for an OpenAI key regardless,
   or move embedding server-side (which means source text reaching the
   platform — a real change to the trust story).

## Vendoring

`vendor/zenik_indexer` is a copy, refreshed by `./sync_vendor.sh`. Installing it
from the private GitHub repo at run time would need a Zenik credential in client
CI — precisely what this architecture avoids. GitHub already fetches this repo
for `uses:`, so the copy is free.
