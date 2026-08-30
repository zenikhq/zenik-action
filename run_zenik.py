#!/usr/bin/env python3
"""zenik-action orchestrator — the PR blast-radius loop, run in the client's CI.

Forked from the prototype's `apply_fix.py`. The prototype's shape (load trigger →
run agent → diff stats → write report → telemetry → GITHUB_OUTPUT) maps directly;
what changed is WHAT runs between: instead of "run an agent to FIX a vendor
change", this does the Zenik v0 loop:

  1. Resolve the PR context (base/head sha, repo, PR number) from the GitHub
     event — or from env overrides for a local/manual run.
  2. Build the derived index of the checkout with the VENDORED zenik-indexer and
     POST it to the platform `/v1/index` (self-contained index, option a — no raw
     source ever leaves; only symbols/edges/embeddings do).
  3. Compute the changed symbols from the PR diff (vendored `changed_symbols`).
  4. POST them to `/v1/impact` and receive the ContextBundle (the blast radius).
     If the platform is unreachable, fall back to computing impact locally from
     the index we just built — a Zenik outage must not blank the client's PR.
  5. Build a FINDINGS prompt (PR diff + bundle) and run the client's coding agent
     (reused agent_backends), sandboxed. It returns PROSE — level-2 guidance, no
     code edits.
  6. Post the findings to the PR as a comment + commit status via the client's
     GITHUB_TOKEN (authored as their own github-actions[bot]).
  7. Report counts-only telemetry to `/v1/telemetry`; gate outputs via
     GITHUB_OUTPUT.

Every external call is best-effort where the prototype made it so (telemetry
never fails the build; a platform blip degrades gracefully). A failed AGENT is a
real failure (exit 1) — but only AFTER telemetry and the comment are written,
mirroring the prototype.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# The vendored indexer travels with this action (see sync_vendor.sh). Import it
# from vendor/ rather than expecting it pip-installed — the client's runner has
# no access to Zenik's private package index.
sys.path.insert(0, str(Path(__file__).parent / "vendor"))
sys.path.insert(0, str(Path(__file__).parent))

import telemetry  # noqa: E402
from agent_backends import AgentResult, select_backend  # noqa: E402
from prompt import build_findings_prompt  # noqa: E402
from report import COMMENT_MARKER, build_report, check_summary  # noqa: E402

from zenik_indexer import build_index, changed_symbols, compute_impact  # noqa: E402
from zenik_indexer.embeddings import get_embedder  # noqa: E402

# The client's repo — in real CI this is the working directory GitHub Actions
# checked the client's code into. For a manual/local run, set CLIENT_REPO_PATH.
CLIENT_REPO = Path(os.environ.get("CLIENT_REPO_PATH", ".")).resolve()

_PLATFORM_TIMEOUT = 30  # index/impact can carry a sizable body; be generous.


# ---------------------------------------------------------------------------
# PR context
# ---------------------------------------------------------------------------
@dataclass
class PRContext:
    full_name: str
    base: Optional[str]
    head: Optional[str]
    pr_number: Optional[int]
    repo_path: Path


def resolve_pr_context() -> PRContext:
    """Figure out which PR we are analysing.

    Real CI: read GITHUB_EVENT_PATH (the pull_request event payload) + env.
    Local/manual run: ZENIK_BASE / ZENIK_HEAD / ZENIK_PR_NUMBER / ZENIK_FULL_NAME
    override everything (mirrors the prototype's TRIGGER_PAYLOAD escape hatch).
    """
    full_name = (os.environ.get("ZENIK_FULL_NAME")
                 or os.environ.get("GITHUB_REPOSITORY") or "")
    base = os.environ.get("ZENIK_BASE") or None
    head = os.environ.get("ZENIK_HEAD") or None
    pr_number = None

    pr_env = os.environ.get("ZENIK_PR_NUMBER")
    if pr_env and pr_env.isdigit():
        pr_number = int(pr_env)

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        try:
            event = json.loads(Path(event_path).read_text())
        except (OSError, json.JSONDecodeError):
            event = {}
        pr = event.get("pull_request") or {}
        if not base:
            base = ((pr.get("base") or {}).get("sha")) or None
        if not head:
            head = ((pr.get("head") or {}).get("sha")) or None
        if pr_number is None and pr.get("number") is not None:
            pr_number = int(pr["number"])
        if not full_name:
            full_name = (event.get("repository") or {}).get("full_name") or ""

    return PRContext(
        full_name=full_name, base=base, head=head,
        pr_number=pr_number, repo_path=CLIENT_REPO,
    )


def git_head_sha(repo_path: Path) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path,
            capture_output=True, text=True, timeout=15,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except Exception:
        return None


def pr_diff_text(repo_path: Path, base: Optional[str], head: Optional[str]) -> str:
    """The raw unified diff for the PR range, for the findings prompt."""
    args = ["diff"]
    if base and head:
        args += [base, head]
    elif base:
        args += [base]
    else:
        args += ["HEAD"]
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo_path,
            capture_output=True, text=True, timeout=120,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Serialization — the indexer's json_store shape (no raw source text).
# ---------------------------------------------------------------------------
def index_payload(result) -> dict:
    """The derived index as the platform's /v1/index expects it.

    Mirrors zenik_indexer.store.json_store.save_index EXACTLY (symbols/edges as
    asdict; chunks as metadata + embedding, never `text`). Kept here rather than
    importing the file writer so the action never touches disk for this.
    """
    return {
        "symbols": [asdict(s) for s in result.symbols],
        "edges": [asdict(e) for e in result.edges],
        "chunks": [
            {
                "path": c.path,
                "language": c.language,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "content_hash": c.content_hash,
                "symbol": c.symbol,
                "embedding": c.embedding,   # text intentionally omitted
            }
            for c in result.chunks
        ],
    }


def changed_payload(changed) -> list[dict]:
    return [c.to_dict() for c in changed]


# ---------------------------------------------------------------------------
# Platform round-trip
# ---------------------------------------------------------------------------
def _platform_post(api_url: str, path: str, body: dict, client_key: str) -> Optional[dict]:
    """POST JSON to the platform with the client bearer key. Returns the parsed
    response, or None on any failure (logged, never raised)."""
    if not api_url:
        print(f"[zenik] no zenik-api-url configured; skipping {path}")
        return None
    url = api_url.rstrip("/") + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client_key}",
            "User-Agent": "zenik-action/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_PLATFORM_TIMEOUT) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:400]
        except Exception:
            pass
        print(f"[zenik] {path} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        print(f"[zenik] {path} -> could not reach platform ({e.reason})")
    except Exception as e:  # noqa: BLE001
        print(f"[zenik] {path} -> unexpected error ({type(e).__name__})")
    return None


def post_index(api_url, client_key, full_name, commit_sha, result) -> Optional[dict]:
    print(f"[zenik] POST /v1/index  ({result.stats()})")
    return _platform_post(api_url, "/v1/index", {
        "full_name": full_name,
        "commit_sha": commit_sha,
        "index": index_payload(result),
    }, client_key)


def post_impact(api_url, client_key, full_name, pr_number, changed) -> Optional[dict]:
    print(f"[zenik] POST /v1/impact  ({len(changed)} changed symbol(s))")
    return _platform_post(api_url, "/v1/impact", {
        "full_name": full_name,
        "pr_number": pr_number,
        "changed": changed_payload(changed),
        "semantic": True,
    }, client_key)


# ---------------------------------------------------------------------------
# The coding agent (reused from the prototype, run for FINDINGS not fixes).
# ---------------------------------------------------------------------------
def run_agent(prompt: str) -> Optional[AgentResult]:
    preference = os.environ.get("AGENT_BACKEND", "").strip() or None
    model = os.environ.get("AGENT_MODEL", "").strip() or None
    try:
        timeout = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "") or 900)
    except ValueError:
        timeout = 900

    backend = select_backend(preference=preference, timeout=timeout, model=model)
    if backend is None:
        return None

    print(f"[zenik] Using agent backend: {backend.name}")
    if not backend.ensure_installed():
        print(f"[zenik] Could not install {backend.package}.")
        return AgentResult(
            backend=backend.name, ok=False,
            error=f"failed to install {backend.package}",
        )

    print(f"[zenik] Handing the FINDINGS task to {backend.name} "
          f"(sandboxed, read-only intent)...")
    started = time.time()
    result = backend.run(prompt, workdir=str(CLIENT_REPO))
    result.duration_seconds = round(time.time() - started, 1)
    if result.ok:
        print(f"[zenik] Agent finished in {result.duration_seconds}s.")
    else:
        print(f"[zenik] Agent did not complete: {result.error}")
    return result


# ---------------------------------------------------------------------------
# Posting to the PR via the client's GITHUB_TOKEN (author = github-actions[bot]).
# ---------------------------------------------------------------------------
def _github_api() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _gh_request(method: str, url: str, token: str, body: Optional[dict] = None) -> Optional[dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "zenik-action/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:400]
        except Exception:
            pass
        print(f"[zenik] github {method} {url} -> HTTP {e.code}: {detail}")
    except Exception as e:  # noqa: BLE001
        print(f"[zenik] github {method} {url} -> {type(e).__name__}")
    return None


def post_pr_comment(full_name: str, pr_number: int, body: str, token: str) -> bool:
    """Create or UPDATE the Zenik comment on the PR (idempotent via the marker)."""
    api = _github_api()
    # Find an existing Zenik comment to update instead of stacking duplicates.
    existing_id = None
    listed = _gh_request(
        "GET",
        f"{api}/repos/{full_name}/issues/{pr_number}/comments?per_page=100",
        token,
    )
    if isinstance(listed, list):
        for c in listed:
            if COMMENT_MARKER in (c.get("body") or ""):
                existing_id = c.get("id")
                break

    if existing_id:
        res = _gh_request(
            "PATCH", f"{api}/repos/{full_name}/issues/comments/{existing_id}",
            token, {"body": body},
        )
        print(f"[zenik] updated existing PR comment {existing_id}")
    else:
        res = _gh_request(
            "POST", f"{api}/repos/{full_name}/issues/{pr_number}/comments",
            token, {"body": body},
        )
        print("[zenik] created PR comment")
    return res is not None


def post_commit_status(full_name: str, sha: str, description: str, token: str) -> bool:
    """Post a commit status (the 'check') on the PR head sha. A commit status
    works with the plain GITHUB_TOKEN, unlike the Checks API which needs a
    GitHub App identity."""
    api = _github_api()
    res = _gh_request(
        "POST", f"{api}/repos/{full_name}/statuses/{sha}", token, {
            "state": "success",
            "context": "zenik/change-impact",
            "description": description[:140],
        },
    )
    return res is not None


def publish_findings(ctx: PRContext, body: str, summary: str) -> bool:
    """Post the comment + status, or (local run / no token) print instead.

    Returns True if something was actually posted to GitHub."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    dry = os.environ.get("ZENIK_DRY_RUN") == "1"

    if dry or not token or not ctx.full_name or ctx.pr_number is None:
        reason = ("ZENIK_DRY_RUN=1" if dry else
                  "no GITHUB_TOKEN / PR context" if not token or ctx.pr_number is None
                  else "no repo")
        print(f"[zenik] not posting to GitHub ({reason}). "
              f"The comment body follows:\n")
        print(body)
        # Also drop it next to the checkout so a local run can inspect it.
        try:
            (ctx.repo_path / "zenik_change_impact.md").write_text(body)
            print("[zenik] wrote zenik_change_impact.md")
        except OSError:
            pass
        return False

    ok_comment = post_pr_comment(ctx.full_name, ctx.pr_number, body, token)
    ok_status = False
    if ctx.head:
        ok_status = post_commit_status(ctx.full_name, ctx.head, summary, token)
    return ok_comment or ok_status


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    started = time.time()
    ctx = resolve_pr_context()
    client_key = os.environ.get("ZENIK_CLIENT_KEY", "")
    api_url = os.environ.get("ZENIK_API_URL", "")

    print(f"[zenik] repo={ctx.full_name or '(unknown)'} "
          f"base={ctx.base} head={ctx.head} pr=#{ctx.pr_number}")

    commit_sha = ctx.head or git_head_sha(ctx.repo_path)

    # 1-2. Build the derived index and POST it to the platform. Embeddings use
    # OPENAI_API_KEY if present (Zenik's key for the demo); otherwise the indexer
    # degrades to its offline embedder (semantic half off) rather than failing.
    embedder = get_embedder()  # reads OPENAI_API_KEY; falls back offline
    print(f"[zenik] building index with embedder: {embedder.name}")
    index = build_index(
        str(ctx.repo_path), embedder=embedder, embed=True,
        commit_sha=commit_sha, progress=True,
    )
    post_index(api_url, client_key, ctx.full_name, commit_sha, index)

    # 3. Changed symbols from the PR diff.
    changed = changed_symbols(str(ctx.repo_path), base=ctx.base, head=ctx.head)
    print(f"[zenik] changed symbols: {len(changed)}")

    # 4. Blast radius from the platform; local fallback if it is unreachable.
    bundle = None
    if changed:
        resp = post_impact(api_url, client_key, ctx.full_name, ctx.pr_number, changed)
        if resp is not None:
            bundle = resp
        else:
            print("[zenik] platform impact unavailable; computing blast radius "
                  "locally from the in-process index (fallback).")
            bundle = compute_impact(index, changed).to_dict()
    else:
        print("[zenik] no changed symbols resolved; empty blast radius.")
        bundle = {"changed": [], "impacted": [], "tests": [], "truncated": False}

    impacted = bundle.get("impacted") or []
    tests = bundle.get("tests") or []
    cross = sum(1 for it in impacted if it.get("cross_service"))

    # 5. Findings agent — prose only, no edits.
    agent_result = None
    outcome = telemetry.OUTCOME_NO_IMPACT
    if impacted or tests:
        prompt = build_findings_prompt(pr_diff_text(ctx.repo_path, ctx.base, ctx.head), bundle)
        agent_result = run_agent(prompt)
        if agent_result is None:
            print("[zenik] No agent key configured (set OPENAI_API_KEY or "
                  "ANTHROPIC_API_KEY). Posting the index's blast radius without "
                  "prose guidance.")
            outcome = telemetry.OUTCOME_REPORTED
        elif not agent_result.ok:
            outcome = telemetry.OUTCOME_AGENT_FAILED
        else:
            outcome = telemetry.OUTCOME_REPORTED
            if not (agent_result.final_message or "").strip():
                # Mirrors the prototype's "loud about an ambiguous no-op" rule.
                print("[zenik] NOTE: the agent completed but produced no findings "
                      "text. The blast-radius list still posts; check the agent "
                      "summary above for why it stayed silent.")
    else:
        print("[zenik] blast radius is empty; skipping the agent.")

    # 6. Build the comment and post it (comment + commit status).
    body = build_report(
        bundle=bundle, agent_result=agent_result, outcome=outcome,
        pr_number=ctx.pr_number, truncated=bool(bundle.get("truncated")),
    )
    summary = check_summary(bundle, outcome)
    publish_findings(ctx, body, summary)

    # 7. Counts-only telemetry.
    duration = round(time.time() - started, 1)
    payload = telemetry.build_payload(
        client_key=client_key,
        repo_full_name=ctx.full_name,
        run_id=telemetry.env_run_id(),
        outcome=outcome,
        pr_number=ctx.pr_number,
        impacted_count=len(impacted),
        changed_count=len(changed),
        cross_service_count=cross,
        duration_seconds=duration,
        agent_result=agent_result,
    )
    telemetry.print_payload(payload)
    telemetry.send(payload, api_url=api_url, client_key=client_key)

    # Gate the client's downstream steps.
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"impacted-count={len(impacted)}\n")
            f.write(f"outcome={outcome}\n")

    print(f"[zenik] Done. outcome={outcome} impacted={len(impacted)} "
          f"cross_service={cross}")

    # A failed agent is a real failure and should be visible in the client's CI,
    # but only AFTER telemetry and the comment are written (prototype parity).
    if outcome == telemetry.OUTCOME_AGENT_FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
