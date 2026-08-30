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

## Walk one PR through the whole loop

A developer on the `meridian` repo edits `exponent_for` — the function that says
"how many decimal places does this currency have." They open a PR. Here is what
`zenik-action` does, step by step.

### Step 1 — Build the index (of the checked-out repo)

The action runs the **vendored** indexer over the checkout:

```
[zenik] building index with embedder: text-embedding-3-small
[indexer] parsed 197 files -> 1368 symbols, 1145 chunks; resolving edges...
[zenik] POST /v1/index  ({'symbols': 1368, 'edges': 4488, 'chunks': 1145})
```

That derived index — names, line numbers, dependency edges, and one embedding
per function — is POSTed to the platform. **The source code itself is never
sent.** (You can prove it: every chunk in the payload has an `embedding` and a
`content_hash`, but no `text` field.)

### Step 2 — Turn the diff into "changed symbols"

The action diffs the PR and maps the changed lines onto whole functions:

```
[zenik] changed symbols: 1
  exponent_for [function] services/reconciliation/app/domain/models.py:55-65 (modified)
```

This is the exact same `changed_symbols` command the indexer exposes — one
function changed.

### Step 3 — Ask the platform for the blast radius

It POSTs those changed symbols to `/v1/impact` and gets back the **context
bundle**: everything that might be affected, fused from two signals —

- **provable edges** — who literally calls `exponent_for`, and
- **semantic look-alikes** — code in *other* services that does the same currency
  math, which no import connects.

```
[zenik] POST /v1/impact  (1 changed symbol(s))
  26 potentially affected site(s), 17 cross-service
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

The findings become a PR **comment** plus a `zenik/change-impact` **commit
status**, authored by the client's own `github-actions[bot]`:

```
## Zenik — change impact

**1** changed symbol(s) → **26** potentially affected site(s) across **2**
service area(s), **17** cross-service ⚠.

### Blast radius
- `services/reconciliation/app/domain/tolerance.py:79` — `tolerance_minor` (calls_maybe)
- `packages/ts-sdk/src/http.ts:128` — `HttpTransport` (semantic) ⚠ cross-service
- `services/ledger-core/src/clients/catalogClient.ts:61` — `CatalogClient` (semantic) ⚠ cross-service
  … 20 more

### Findings & guidance
> the agent's prose about each affected site …
```

Re-running on the same PR **updates** that comment instead of stacking a new one
(it finds its own comment by a hidden marker).

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
build_index (vendored)  ──POST──▶  /v1/index      (derived index, no source)
   │
changed_symbols (diff)  ──POST──▶  /v1/impact  ──▶ context bundle (blast radius)
   │                                                   │
   ▼                                                   ▼
build_findings_prompt(diff + bundle)  ──▶  coding agent (Codex/Claude)  ──▶ prose
   │
   ▼
PR comment + commit status (client's GITHUB_TOKEN)     /v1/telemetry (counts only)
```

If `/v1/impact` can't be reached, the action computes the blast radius **locally**
from the index it just built — same engine, so the PR still gets an answer.

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

With `ZENIK_DRY_RUN=1` and no `ZENIK_API_URL`, it builds the index, computes the
blast radius locally, runs the agent if a key is set, and writes the comment to
`zenik_change_impact.md` next to the checkout instead of posting to GitHub.

---

## What's deliberately NOT here (v0)

- **No code fixes** — prose findings only (level 2). The agent never edits files.
- **No test-running / verification** of a fix — that is the post-v0 differentiator.
- **Full rebuild each run** — no incremental re-embedding yet.
- **The live agent + live PR comment run only in real CI** (a Linux runner with a
  real PR and a real `GITHUB_TOKEN`). Everything else — indexing, diff→symbols,
  the platform round-trip, the findings prompt, the comment body — runs anywhere.
