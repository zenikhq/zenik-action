# zenik-action — Demo Walkthrough

Plain-English explanation of what this repo does, how it works, and how to show
it. For the *why* behind the product, see `../ARCHITECTURE.md` and `../BUILD.md`.
For the engine it runs, see `../zenik-indexer/DEMO.md`; for the server it calls,
`../zenik-platform/DEMO.md`.

---

## What it does, in one line

It is the piece that runs **inside the client's own CI**. When someone opens a
pull request, it asks Zenik *"what else can this change break?"* and posts the
answer back on the PR — in the client's own repo, using the client's own keys.

The other three repos are the brain (`zenik-indexer`), the memory
(`zenik-platform`), and the screen (`zenik-dashboard`). This one is the **hands**:
it does the work where the client's code lives, so the code never leaves.

---

## Two modes, one action

The same action runs on two triggers, doing two different jobs:

- **`push` to the default branch (and `workflow_dispatch`)** — *keep the graph
  fresh.* The stored dependency graph on the platform mirrors **main**. On every
  merge, the action re-parses the repo (seconds), asks the platform which chunk
  hashes it already has (`GET /v1/index/manifest`), embeds **only the new or
  changed chunks**, and uploads — unchanged chunks ship without vectors and the
  platform reuses the stored ones. A steady-state refresh looks like this:

  ```
  [zenik] 1145 chunk(s) parsed; 1114 already stored; 0 to embed
  [zenik] index updated: {'symbols': 1368, 'edges': 4488, 'chunks': 1145, 'embeddings_reused': 1145}
  ```

  Zero embedding calls, a tiny upload, done in under a minute. The **first-ever**
  push run finds an empty manifest and indexes everything — that IS onboarding:
  merging the workflow file itself triggers it.

- **`pull_request`** — *query the graph, write nothing.* A PR run never uploads
  an index, so PRs can't pollute the graph and cost scales with the diff, not
  the repo.

---

## Walk one PR through the query loop

A developer on the `meridian` repo edits `exponent_for` — the function that says
"how many decimal places does this currency have." They open a PR. Here is what
`zenik-action` does, step by step.

### Step 1 — Parse the checkout (no embedding, no upload)

```
[zenik] query mode: parsing checkout (no index upload)
[indexer] parsed 197 files -> 1368 symbols, 1145 chunks; resolving edges...
```

The parse is cheap (tree-sitter, a few seconds). It exists so the run can find
the changed function's chunk text and so there is a local fallback if the
platform is unreachable. **The source code itself never leaves the runner.**

### Step 2 — Turn the diff into "changed symbols"

The action diffs the PR and maps the changed lines onto whole functions:

```
[zenik] changed symbols: 1
  exponent_for [function] services/reconciliation/app/domain/models.py:55-65 (modified)
```

This is the exact same `changed_symbols` command the indexer exposes — one
function changed.

### Step 3 — Ask the platform for the blast radius

It embeds JUST the changed chunk (one OpenAI call — the PR's new code isn't in
the stored graph, so the semantic search needs its fresh vector) and POSTs the
changed symbols + that query vector to `/v1/impact`. Back comes the **context
bundle**: everything that might be affected, fused from two signals —

- **provable edges** — who literally calls `exponent_for`, and
- **semantic look-alikes** — code in *other* services that does the same currency
  math, which no import connects.

```
[zenik] POST /v1/impact  (1 changed symbol(s), 1 fresh query vector(s))
  27 potentially affected site(s), 19 cross-service
```

The cross-service ones are the point: a human reviewing this PR sees the direct
callers easily. What they'd *miss* is that four other services carry their own
copy of this currency logic that could now silently disagree.

### Step 4 — The agent writes findings (prose, not code)

The action hands the PR diff + the bundle to the client's coding agent (Codex or
Claude Code) with a **findings** prompt. The agent reads the real code at each
site and writes guidance: *what* is affected and *why it's risky*. It is told, in
several ways, **not to edit anything** — this is a report, not a fix.

> **What Zenik surfaces vs. what a fix would do:** in v0 the agent stops at
> "level 2" — prose. It says *"this cross-service copy in `ledger-core` still
> assumes 2 decimal places and will disagree for JPY."* It does **not** write the
> patch. That is deliberate (BUILD.md §5): v0 exists to prove the blast-radius
> answer is correct before we let the agent change code again.

### Step 5 — Post it on the PR

The findings land on the PR in three pieces, all authored by the client's own
`github-actions[bot]`:

1. **Inline review comments on the changed code.** Each changed symbol gets one
   short comment pinned on its own diff hunk — headline caller counts, the
   agent's note in plain English (1–3 sentences, states the problem directly),
   and the caller list folded into a `<details>`:

   ```
   Zenik — `exponent_for`: 6 caller(s), 1 cross-service ⚠.

   Callers that pass strict=True will now raise ValueError on unknown
   currencies. to_major uses the default and is safe.

   ▸ Callers
   ```

   (GitHub only allows inline comments on lines that are in the diff — and the
   callers themselves are *unchanged* code — so the comments pin to the changed
   symbol's hunk and point outward at its callers.)

2. **One short summary comment** — the counts headline, likely-relevant tests,
   and a **"Parallel implementations — keep in sync"** section for code the
   agent found that independently implements the same logic (not blast radius,
   not noise — a reminder), plus the trust footer.

3. A `zenik/change-impact` **commit status**.

4. **A 3-4 line block at the bottom of the PR description** — the counts
   headline plus, when the agent flags one, an intent-mismatch warning ("the
   description says docs-only but the diff changes `charge()`"). The block sits
   between hidden `zenik:impact` markers, so the author's own text is **never
   modified** — re-runs replace only Zenik's block. This is deliberately not
   how CodeRabbit (appends into the body) or Qodo `/describe` (rewrites it)
   behave; overwriting an author's description is the most-complained-about
   habit of PR bots. Turn it off entirely with `update-pr-description: "false"`.
   `/zenik fix` never touches the description.

The agent also reads the PR title + description as the author's **stated
intent** and compares it against what the diff actually does — a clear
divergence shows up as a warning in both the summary comment and the
description block.

Re-running on the same PR replaces its own output instead of stacking: the
summary comment is updated in place, and the previous run's inline comments are
found (by a hidden marker), deleted, and re-posted fresh. If the inline review
can't be posted for any reason, everything folds back into a single full
comment — the old layout — so the report is never lost.

### Step 6 — Report counts, never names

Finally it sends telemetry — and this is where the trust model is enforced in
code. The entire body is counts and enums:

```json
{ "outcome": "reported", "changed_count": 1, "impacted_count": 26,
  "cross_service_count": 17, "repo_hash": "sha256:fb2c75ae…",
  "agent": {"backend": "claude-code", ...}, "usage": {"input_tokens": ...} }
```

No file names, no paths, no symbol names, no source. The repo is a **salted
hash**, not its name. The whole payload is printed in the job log so the client
can see exactly what left their runner.

---

## How the pieces fit

```
PR opened
   │
   ▼
push to main:
  build_index(embed=False) ──GET──▶ /v1/index/manifest (stored chunk hashes)
  embed ONLY new hashes    ──POST─▶ /v1/index          (delta; platform reuses
                                                        stored embeddings)
pull_request:
  build_index(embed=False)          (parse only — nothing uploaded)
  changed_symbols (diff) + fresh query vectors
                           ──POST─▶ /v1/impact ──▶ context bundle (blast radius)
                                                       │
                                                       ▼
build_findings_prompt(diff + bundle)  ──▶  coding agent (Codex/Claude)  ──▶ prose
   │
   ▼
inline review comments + summary comment + commit status   /v1/telemetry (counts)
```

If `/v1/impact` can't be reached (or the repo was never indexed — merge/run the
workflow on main first), the action computes a **deterministic-only** blast
radius locally from the parse — the PR still gets an answer, minus the semantic
layer.

---

## What's reused, and what's new

Forked from the prior prototype `../../zenik-proto/api-fix-action`:

- **Reused near-verbatim:** `agent_backends.py` (the Codex + Claude Code adapters,
  pinned CLI versions, key-scoping, sandbox detection) and `telemetry.py` (the
  counts-only, best-effort, salted-hash discipline — only the endpoint and the
  field set changed for a PR run).
- **Rewritten:** `prompt.py` now builds a *findings* prompt instead of a *fix*
  prompt; `report.py` writes a blast-radius comment instead of a fix report; the
  orchestrator went from `apply_fix.py` (run agent → edit code → open PR) to
  `run_zenik.py` (index → impact → findings → comment).
- **Dropped:** the prototype's `legacy_scan.py` regex fallback.

---

## How to run the demo yourself

The action is meant to run in GitHub CI, but every piece is exercisable locally.
The orchestrator honours env overrides so you can point it at any checkout:

```bash
cd zenik-action
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Re-vendor the indexer if it changed:
./sync_vendor.sh

# Dry run against a local checkout (prints the comment instead of posting):
CLIENT_REPO_PATH=/path/to/meridian \
ZENIK_FULL_NAME=acme/meridian \
ZENIK_PR_NUMBER=1 \
ZENIK_DRY_RUN=1 \
OPENAI_API_KEY=sk-...           # for embeddings + the Codex agent (optional) \
  python run_zenik.py
```

With `ZENIK_DRY_RUN=1` and no `ZENIK_API_URL`, it parses the checkout, computes
a deterministic-only blast radius locally, runs the agent if a key is set, and
writes the comment to `zenik_change_impact.md` next to the checkout instead of
posting to GitHub. Add `ZENIK_MODE=index` to exercise the graph-refresh mode
instead (parse → manifest → embed-new-only → upload).

---

## What's deliberately NOT here (v0)

- **No code fixes** — prose findings only (level 2). The agent never edits files.
- **No test-running / verification** of a fix — that is the post-v0 differentiator.
- **Full rebuild each run** — no incremental re-embedding yet.
- **The live agent + live PR comment run only in real CI** (a Linux runner with a
  real PR and a real `GITHUB_TOKEN`). Everything else — indexing, diff→symbols,
  the platform round-trip, the findings prompt, the comment body — runs anywhere.
