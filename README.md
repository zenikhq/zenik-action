# zenik-action

Zenik's client-side GitHub Action. A client adds it to their repo and, on every
pull request, Zenik computes the **blast radius** of the change — what else it
might break — and posts prose findings + per-caller guidance back on the PR.

It runs **inside the client's own GitHub Actions CI**, on the client's runner,
with the client's keys. There is no Zenik-hosted runner that touches their
code. Source never leaves the client's environment; only a *derived* index
(symbols, edges, embeddings — never source text) is POSTed to the Zenik
platform. The [Zenik GitHub App](https://github.com/apps/zenik-ai) exists only so
the findings post as `zenik-ai[bot]` — it has **no code access** (pull requests +
checks write, metadata read), and the install screen says so.

See `DEMO.md` for an example-driven walkthrough, and `../BUILD.md` §5/§6/§10.1
for the v0 scope and the machine→platform trust path.

---

## What it does (one run)

On `on: pull_request`, in the client's CI:

1. **Builds the derived index** of the checked-out repo with the **vendored**
   `zenik-indexer` (see `vendor/`).
2. **POSTs the index** to the platform `/v1/index` (no raw source — symbols,
   edges, and chunk embeddings only).
3. **Computes the PR's changed symbols** from the diff (`zenik-indexer`'s
   `changed_symbols`) and **POSTs them to `/v1/impact`**, receiving the blast
   radius (the "context bundle").
4. **Runs the client's coding agent** (Codex or Claude Code, reused from the
   prior prototype) to turn that bundle into **prose findings + per-caller
   guidance** — level 2. It produces text only; **it does not edit code** in v0.
5. **Posts the findings to the PR** — inline review comments, a summary
   comment, a block in the PR description, and a **"Zenik" check run** with
   per-symbol annotations — all as `zenik-ai[bot]` via the Zenik GitHub App. The
   check never blocks a merge on findings (`neutral` with findings, `success`
   without; `failure` only if Zenik itself broke).
6. **Reports counts-only telemetry** to `/v1/telemetry` (never names or source).

If the Zenik platform is unreachable, the action falls back to computing the
blast radius **locally** from the index it just built, so a Zenik outage never
blanks the PR.

---

## Usage

Two steps, no Zenik secret:

1. **Install the Zenik GitHub App** on the repo:
   <https://github.com/apps/zenik-ai>. The install screen shows exactly what it
   can do — pull requests, checks, metadata; no code access.
2. **Merge the workflow** below as `.github/workflows/zenik.yml` (full example
   in [`examples/zenik.yml`](examples/zenik.yml)). The merge itself triggers
   the first indexing run. Done.

```yaml
name: Zenik change impact
on:
  pull_request:
permissions:
  id-token: write               # Zenik authenticates to its platform with GitHub's OIDC token
  contents: read                # actions/checkout (write only if you enable `/zenik fix`)
jobs:
  zenik:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0            # so the PR base commit is available to diff
      - uses: zenikhq/zenik-action@v1
        with:
          zenik-api-url: https://api.zenik.dev
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

### Required secrets

| Secret | Purpose |
|---|---|
| `OPENAI_API_KEY` | Chunk embeddings **and** the Codex agent backend. |
| `ANTHROPIC_API_KEY` | The Claude Code agent backend. |

Provide at least one agent key. The blast radius has two halves: the
**deterministic** half — everything that literally imports or calls your change —
and the **semantic** half — code that *does a similar thing* in another service or
language (a re-implemented helper no import points to), found via embeddings.
Embeddings need OpenAI, so if you run Claude Code as the agent but still want the
semantic half, set `OPENAI_API_KEY` too. Without any OpenAI key on the runner,
indexing falls back to an offline embedder and the semantic half is simply off —
the deterministic callers are still reported.

---

## How the action authenticates

One code path, no fallback (`platform_auth.py`):

- **To the platform: GitHub Actions OIDC.** With `permissions: id-token: write`
  the runner can mint a short-lived JWT that says which repo this job runs in.
  Every platform call (`/v1/index`, `/v1/impact`, `/v1/telemetry`,
  `/v1/github/token`) sends a fresh one as its bearer; the platform resolves
  the repo from the token's `repository_id` claim. Nothing to rotate, nothing
  to leak.
- **To GitHub: the Zenik GitHub App.** The platform holds the app's private
  key and hands the action a one-hour installation token for this repo
  (`POST /v1/github/token`). Every write — the summary comment, the inline
  review, the description block, the check run, `/zenik fix` replies — goes
  through it, so it all appears as **`zenik-ai[bot]`**. The app deliberately has
  **no contents permission**.
- **The workflow's own `GITHUB_TOKEN`** does one thing: `/zenik fix` pushes its
  commit with it (the commit itself is authored as `zenik-ai[bot]`). The action
  never posts with it.

If the app isn't installed on the repo, the run fails with a `::error::` and
the install link — it does not quietly post as `github-actions[bot]` instead.

---

## Inputs

| Input | Default | Description |
|---|---|---|
| `openai-api-key` | `""` | OpenAI key — embeddings + Codex backend. |
| `anthropic-api-key` | `""` | Anthropic key — Claude Code backend. |
| `agent-backend` | `""` | `codex` or `claude-code`. Defaults to whichever key is set (codex if both). |
| `agent-model` | `""` | Optional model override passed to the agent CLI. |
| `agent-timeout-seconds` | `900` | Wall-clock limit for the agent run. |
| `zenik-api-url` | `""` | Base URL of the Zenik platform (OIDC-authenticated; no secret). |
| `update-pr-description` | `"true"` | Append/refresh Zenik's marker-bounded block at the bottom of the PR description. |
| `github-token` | `${{ github.token }}` | Used **only** by `/zenik fix` to push its commit. Never used to post. |

## Outputs

| Output | Description |
|---|---|
| `impacted-count` | Number of potentially affected sites surfaced. `0` = no impact found. |
| `outcome` | One of `reported`, `no_impact`, `agent_failed`. |

---

## Keys are scoped to the step, never the job

Every API key is passed as an **input** and set as `env:` on the single step
that needs it — never as a job-level env var. Repository-controlled code running
in the same job (build scripts, other actions) cannot read them. The agent runs
**sandboxed** (Codex `workspace-write`, Claude Code `acceptEdits`); if the
sandbox can't start, the run fails rather than proceeding unconfined. These
behaviors are inherited near-verbatim from the prior prototype's
`agent_backends.py` — see the comments in that file.

---

## The vendored indexer

The action runs on the **client's** runner, where we deliberately expose no
token that could pull Zenik's private packages. So `zenik-indexer` is
**vendored** — copied into `vendor/zenik_indexer/` and committed — rather than
pip-installed from git. `run_zenik.py` adds `vendor/` to `sys.path` and imports
it there.

Re-vendor after the indexer changes:

```bash
./sync_vendor.sh                 # copies ../zenik-indexer/zenik_indexer into vendor/
                                 # and pins the source commit in vendor/INDEXER_VERSION
git add vendor && git commit     # ship the new copy with the action
```

`requirements.txt` carries the indexer's *runtime* deps (tree-sitter +
language-pack, and `openai` for embeddings). It deliberately omits `psycopg`:
the action never talks to Postgres — it POSTs the derived index over HTTP.

---

## Files

| File | Role |
|---|---|
| `action.yml` | The composite action: setup, grammar prefetch, Codex sandbox prep, the run step. |
| `run_zenik.py` | Orchestrator — the PR blast-radius loop (forked from the prototype's `apply_fix.py`). |
| `platform_auth.py` | OIDC token → platform calls; the Zenik GitHub App installation token; fail-loud errors. |
| `fix.py` | The `/zenik fix` opt-in fix agent (gated; commits as `zenik-ai[bot]`, pushes with the workflow token). |
| `agent_backends.py` | Codex / Claude Code adapters (reused verbatim from the prototype). |
| `telemetry.py` | Counts-only, best-effort telemetry (reused; re-pointed at `/v1/telemetry`). |
| `prompt.py` | Builds the **findings** prompt (PR diff + blast radius). |
| `report.py` | Builds the PR-comment bodies and the check-run payload. |
| `tests/` | Unit tests (`python -m pytest -q`). |
| `sync_vendor.sh` + `vendor/` | Vendoring script + the committed indexer copy. |
| `examples/zenik.yml` | A ready-to-copy client workflow. |

---

## What's deliberately NOT here (v0)

- **No code fixes.** The agent writes prose guidance (level 2), never diffs.
  Remediation is a proven, post-v0 capability (the prior prototype did it).
- **No verification loop** — running the affected tests after a fix comes later.
- **Full index rebuild each run** — incremental re-embedding is a fast-follow.
- **Single repo** — cross-repo blast radius is out of scope for v0.
