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
  6. Post the findings to the PR — inline review, summary comment, description
     block, and a "Zenik" check run — as `zenik-ai[bot]`, via the Zenik GitHub
     App's installation token (fetched from the platform; see platform_auth).
  7. Report counts-only telemetry to `/v1/telemetry`; gate outputs via
     GITHUB_OUTPUT.

Auth (platform_auth.py): the action proves itself to the platform with the
job's GitHub OIDC token — no Zenik secret — and posts to GitHub ONLY with the
app token. There is no fallback to the workflow's GITHUB_TOKEN for posting: if
the app isn't installed on the repo, the run fails loudly with the install
link. The workflow token's one remaining job is `/zenik fix`'s push (fix.py).

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

import platform_auth  # noqa: E402
import telemetry  # noqa: E402
from agent_backends import AgentResult, select_backend  # noqa: E402
from platform_auth import InstallationToken, PlatformAuthError  # noqa: E402
from prompt import build_findings_prompt  # noqa: E402
from report import (CHECK_RUN_NAME, COMMENT_MARKER, INLINE_MARKER,  # noqa: E402
                    build_check_run, build_inline_body,
                    build_description_block, build_report, callers_of,
                    merge_description, note_for, parse_agent_message,
                    strip_description_block)

from zenik_indexer import build_index, changed_symbols, compute_impact  # noqa: E402
from zenik_indexer.embeddings import get_embedder  # noqa: E402

# The client's repo — in real CI this is the working directory GitHub Actions
# checked the client's code into. For a manual/local run, set CLIENT_REPO_PATH.
CLIENT_REPO = Path(os.environ.get("CLIENT_REPO_PATH", ".")).resolve()

_PLATFORM_TIMEOUT = 120     # impact/telemetry: loads the stored index server-side
_INDEX_UPLOAD_TIMEOUT = 600  # /v1/index ships the whole embedded index (tens of MB)


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
    pr_title: str = ""
    pr_body: str = ""
    from_fork: bool = False   # GitHub withholds the OIDC token on fork PRs


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
    pr_title = ""
    pr_body = ""
    from_fork = False

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
        pr_title = (pr.get("title") or "").strip()
        pr_body = pr.get("body") or ""
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        from_fork = bool(pr and head_repo and full_name and head_repo != full_name)

    return PRContext(
        full_name=full_name, base=base, head=head,
        pr_number=pr_number, repo_path=CLIENT_REPO,
        pr_title=pr_title, pr_body=pr_body, from_fork=from_fork,
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
# Inline anchoring — which diff lines an inline review comment may pin to.
# ---------------------------------------------------------------------------
def diff_anchor_lines(diff_text: str) -> dict[str, dict[str, list[int]]]:
    """Per file path, the diff's anchorable lines: {'added': [new-side line
    numbers], 'deleted': [old-side line numbers]}.

    GitHub review comments only attach to lines present in the PR diff — added
    lines pin with side RIGHT, deleted lines with side LEFT (needed for
    deletion-only changes, which have no added lines at all). An invalid anchor
    422s the whole review, so we only ever pin to lines this parse proves are
    in the diff.
    """
    anchors: dict[str, dict[str, list[int]]] = {}
    path = None
    old_path = None
    new_line = 0
    old_line = 0
    in_hunk = False
    for raw in (diff_text or "").splitlines():
        if raw.startswith("diff --git"):
            # New file section: without this reset, the next file's `--- a/...`
            # header would be misread as a deleted line of the previous hunk.
            in_hunk = False
            path = None
            old_path = None
        elif not in_hunk and raw.startswith("--- "):
            target = raw[4:].strip()
            old_path = target[2:] if target.startswith("a/") else (
                None if target == "/dev/null" else target)
        elif raw.startswith("+++ "):
            target = raw[4:].strip()
            # A fully deleted file (`+++ /dev/null`) is still anchorable on
            # side LEFT via its old path.
            path = target[2:] if target.startswith("b/") else (
                old_path if target == "/dev/null" else target)
            in_hunk = False
        elif raw.startswith("@@"):
            # @@ -a,b +c,d @@ — a/c are the first old/new-side hunk lines.
            try:
                old_part = raw.split("-", 1)[1].split(" ", 1)[0]
                new_part = raw.split("+", 1)[1].split(" ", 1)[0]
                old_line = int(old_part.split(",", 1)[0])
                new_line = int(new_part.split(",", 1)[0])
                in_hunk = path is not None
            except (IndexError, ValueError):
                in_hunk = False
        elif in_hunk:
            if raw.startswith("+"):
                anchors.setdefault(path, {"added": [], "deleted": []})[
                    "added"].append(new_line)
                new_line += 1
            elif raw.startswith("-"):
                anchors.setdefault(path, {"added": [], "deleted": []})[
                    "deleted"].append(old_line)
                old_line += 1
            elif raw.startswith("\\"):
                pass  # "no newline" marker: belongs to no side
            else:
                new_line += 1  # context line exists on both sides
                old_line += 1
    return anchors


def build_inline_candidates(bundle: dict, structured, diff_text: str) -> list[dict]:
    """One inline comment per changed symbol that has callers AND an anchorable
    diff line inside its span — an added line (side RIGHT) when the change adds
    code, a deleted line (side LEFT) for deletion-only changes. Symbols that
    miss either fold back into the summary comment."""
    anchors = diff_anchor_lines(diff_text)
    out = []
    for ch in bundle.get("changed") or []:
        callers = callers_of(bundle, ch.get("name"))
        if not callers:
            continue
        file_anchors = anchors.get(ch.get("path")) or {"added": [], "deleted": []}
        start = ch.get("start_line") or 0
        end = ch.get("end_line") or 0
        anchor = next((ln for ln in file_anchors["added"] if start <= ln <= end), None)
        side = "RIGHT"
        if anchor is None:
            # Deletion-only change: pin to a removed line. Old-side numbers vs
            # the symbol's new-side span is the same approximation the indexer
            # itself uses to flag the symbol as changed.
            anchor = next((ln for ln in file_anchors["deleted"]
                           if start <= ln <= end), None)
            side = "LEFT"
        if anchor is None:
            print(f"[zenik] no anchorable diff line for `{ch.get('name')}` "
                  f"({ch.get('path')}); its detail stays in the summary comment")
            continue
        out.append({
            "path": ch.get("path"),
            "line": anchor,
            "side": side,
            "body": build_inline_body(ch, callers, note_for(structured, ch.get("name"))),
        })
    return out


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
# Platform round-trip — every call carries a fresh OIDC bearer (platform_auth).
# The platform resolves the repo from the token's `repository_id` claim;
# `full_name` still travels in bodies its models expect it in (it validates
# the two agree) but is never how we are identified.
# ---------------------------------------------------------------------------
def _platform_post(api_url: str, path: str, body: dict,
                   timeout: int = _PLATFORM_TIMEOUT) -> Optional[dict]:
    return platform_auth.platform_request(api_url, path, body, "POST", timeout)


def fetch_manifest(api_url, full_name) -> Optional[dict]:
    """What the platform already holds for this repo (chunk content hashes)."""
    from urllib.parse import quote
    return platform_auth.platform_request(
        api_url, f"/v1/index/manifest?full_name={quote(full_name, safe='')}",
        None, "GET")


def post_index(api_url, full_name, commit_sha, result) -> Optional[dict]:
    print(f"[zenik] POST /v1/index  ({result.stats()})")
    return _platform_post(api_url, "/v1/index", {
        "full_name": full_name,
        "commit_sha": commit_sha,
        "index": index_payload(result),
    }, timeout=_INDEX_UPLOAD_TIMEOUT)


def post_impact(api_url, full_name, pr_number, changed,
                query_vectors=None) -> Optional[dict]:
    print(f"[zenik] POST /v1/impact  ({len(changed)} changed symbol(s), "
          f"{len(query_vectors or {})} fresh query vector(s))")
    return _platform_post(api_url, "/v1/impact", {
        "full_name": full_name,
        "pr_number": pr_number,
        "changed": changed_payload(changed),
        "semantic": True,
        "query_vectors": query_vectors,
    })


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
# Posting to the PR as zenik-ai[bot]. `token` is ALWAYS the Zenik GitHub App's
# installation token (InstallationToken.bearer()) — never GITHUB_TOKEN.
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


def post_pr_review(full_name: str, pr_number: int, head_sha: str,
                   comments: list[dict], token: str) -> bool:
    """Post the inline findings as ONE review (event COMMENT). Reviews can't be
    PATCHed like issue comments, so re-runs first delete the previous run's
    inline comments (found via INLINE_MARKER) and post fresh."""
    api = _github_api()
    listed = _gh_request(
        "GET",
        f"{api}/repos/{full_name}/pulls/{pr_number}/comments?per_page=100",
        token,
    )
    if isinstance(listed, list):
        for c in listed:
            if INLINE_MARKER in (c.get("body") or ""):
                _gh_request(
                    "DELETE",
                    f"{api}/repos/{full_name}/pulls/comments/{c.get('id')}",
                    token,
                )

    res = _gh_request(
        "POST", f"{api}/repos/{full_name}/pulls/{pr_number}/reviews", token, {
            "commit_id": head_sha,
            "event": "COMMENT",
            "comments": [
                {"path": c["path"], "line": c["line"], "side": c["side"],
                 "body": c["body"]}
                for c in comments
            ],
        },
    )
    if res is not None:
        print(f"[zenik] posted review with {len(comments)} inline comment(s)")
        return True
    print("[zenik] review post failed; folding detail into the summary comment")
    return False


def create_check_run(full_name: str, head_sha: str, token: str) -> Optional[int]:
    """Open the "Zenik" check run as in_progress at the start of the run, so
    the PR shows Zenik is working. Returns the check-run id (None on failure;
    the completed result is then POSTed fresh at the end)."""
    api = _github_api()
    res = _gh_request(
        "POST", f"{api}/repos/{full_name}/check-runs", token, {
            "name": CHECK_RUN_NAME,
            "head_sha": head_sha,
            "status": "in_progress",
            "output": {"title": "Zenik is computing the blast radius…",
                       "summary": "Results will appear here and in the PR comments."},
        },
    )
    check_id = res.get("id") if isinstance(res, dict) else None
    if check_id:
        print(f"[zenik] opened check run {check_id} (in_progress)")
    return check_id


def complete_check_run(full_name: str, check_id: Optional[int],
                       payload: dict, token: str) -> bool:
    """Finish the check run: PATCH the one opened at the start, or POST a
    completed one if that failed. `payload` comes from report.build_check_run."""
    api = _github_api()
    if check_id:
        res = _gh_request(
            "PATCH", f"{api}/repos/{full_name}/check-runs/{check_id}", token,
            {k: v for k, v in payload.items() if k != "head_sha"},
        )
    else:
        res = _gh_request("POST", f"{api}/repos/{full_name}/check-runs",
                          token, payload)
    if res is None:
        print("[zenik] check run update failed (non-fatal)")
        return False
    n = len((payload.get("output") or {}).get("annotations") or [])
    print(f"[zenik] check run completed: {payload.get('conclusion')} "
          f"({n} annotation(s))")
    return True


def fail_check_run(full_name: str, check_id: Optional[int], head_sha: str,
                   message: str, token: str) -> None:
    """Zenik itself broke: mark the check as failure so the PR says so."""
    complete_check_run(full_name, check_id, {
        "name": CHECK_RUN_NAME, "head_sha": head_sha,
        "status": "completed", "conclusion": "failure",
        "output": {"title": "Zenik did not complete",
                   "summary": message[:1000]},
    }, token)


def update_pr_description(ctx: PRContext, bundle: dict, structured,
                          token: str) -> bool:
    """Append/refresh Zenik's marker-bounded block at the BOTTOM of the PR
    description. The author's own text is never modified — only the region
    between the zenik:impact markers is replaced (CodeRabbit/Qodo-style body
    rewrites are exactly what this avoids). `/zenik fix` never calls this.
    Opt out with the `update-pr-description: "false"` action input."""
    if os.environ.get("ZENIK_UPDATE_PR_DESCRIPTION", "true").lower() in (
            "false", "0", "no"):
        print("[zenik] PR-description block disabled by input")
        return False
    base = f"{_github_api()}/repos/{ctx.full_name}/pulls/{ctx.pr_number}"
    current = _gh_request("GET", base, token)
    if current is None:
        print("[zenik] could not read the PR body; leaving the description alone")
        return False
    block = build_description_block(bundle, structured)
    new_body = merge_description(current.get("body"), block)
    if new_body == (current.get("body") or ""):
        return True  # idempotent re-run, nothing to write
    res = _gh_request("PATCH", base, token, {"body": new_body})
    if res is None:
        print("[zenik] PR-description update failed (non-fatal)")
        return False
    print("[zenik] refreshed the Zenik block in the PR description")
    return True


def publish_findings(ctx: PRContext, body: str, token: Optional[str]) -> bool:
    """Post the summary comment as zenik-ai[bot], or (dry run / no PR context —
    `token` is None) print it instead.

    Returns True if something was actually posted to GitHub."""
    dry = os.environ.get("ZENIK_DRY_RUN") == "1"

    if not token or not ctx.full_name or ctx.pr_number is None:
        reason = ("ZENIK_DRY_RUN=1" if dry else
                  "no PR context" if ctx.pr_number is None
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

    return post_pr_comment(ctx.full_name, ctx.pr_number, body, token)


# ---------------------------------------------------------------------------
# Index-update mode (push to the default branch): keep the stored graph fresh.
# ---------------------------------------------------------------------------
def run_index_update(ctx: PRContext, api_url: str, started: float) -> None:
    """The graph lives on the platform and mirrors the DEFAULT branch. This
    runs on `push` (and workflow_dispatch): parse everything (cheap), ask the
    platform which chunk hashes it already has, embed ONLY the new ones, and
    upload — unchanged chunks ship without vectors and the platform reuses the
    stored ones. The first-ever run has an empty manifest, so it embeds and
    ships everything: that IS onboarding."""
    commit_sha = ctx.head or git_head_sha(ctx.repo_path)
    print("[zenik] index-update mode: refreshing the stored graph "
          f"(commit {commit_sha[:9] if commit_sha else '?'})")
    index = build_index(str(ctx.repo_path), embed=False,
                        commit_sha=commit_sha, progress=True)

    manifest = fetch_manifest(api_url, ctx.full_name) or {}
    known = set(manifest.get("content_hashes") or [])
    new_chunks = [c for c in index.chunks
                  if c.content_hash not in known and (c.text or "").strip()]
    print(f"[zenik] {len(index.chunks)} chunk(s) parsed; "
          f"{len(known)} already stored; {len(new_chunks)} to embed")

    if new_chunks:
        embedder = get_embedder()  # reads OPENAI_API_KEY; falls back offline
        if embedder.name == "hash-fallback" and known:
            # Mixing offline hash vectors into an OpenAI-embedded graph would
            # corrupt the NN math; ship the new chunks vector-less instead.
            print("[zenik] WARNING: no OPENAI_API_KEY — new chunks upload "
                  "without embeddings (semantic misses new code until a keyed run)")
        else:
            print(f"[zenik] embedding {len(new_chunks)} chunk(s) "
                  f"with {embedder.name}")
            vectors = embedder.embed([c.text or "" for c in new_chunks])
            for c, v in zip(new_chunks, vectors):
                c.embedding = v

    resp = post_index(api_url, ctx.full_name, commit_sha, index)
    if resp is not None:
        print(f"[zenik] index updated: {resp.get('stats')}")

    payload = telemetry.build_payload(
        run_id=telemetry.env_run_id(), outcome=telemetry.OUTCOME_INDEXED,
        pr_number=None, impacted_count=0, changed_count=len(new_chunks),
        cross_service_count=0,
        duration_seconds=round(time.time() - started, 1), agent_result=None,
    )
    telemetry.print_payload(payload)
    telemetry.send(payload, api_url=api_url)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write("impacted-count=0\n")
            f.write(f"outcome={telemetry.OUTCOME_INDEXED}\n")
    print(f"[zenik] Done. outcome={telemetry.OUTCOME_INDEXED} "
          f"new_chunks={len(new_chunks)}")


def compute_query_vectors(index, changed) -> Optional[dict]:
    """Fresh embeddings for the changed chunks, keyed by changed-symbol name.

    The PR's new code isn't in the stored graph, so the semantic search needs
    its vectors from here. A few chunks per PR — cents, not a full re-embed.
    Returns None (engine falls back to stored base-version vectors) when no
    OpenAI key is set: offline hash vectors don't live in the same space as
    the stored graph."""
    embedder = get_embedder()
    if embedder.name == "hash-fallback":
        return None
    pairs = []
    for ch in changed:
        chunk = next(
            (c for c in index.chunks
             if c.path == ch.path and (c.text or "").strip()
             and not (c.end_line < ch.start_line or c.start_line > ch.end_line)),
            None,
        )
        if chunk is not None:
            pairs.append((ch.name, chunk.text or ""))
    if not pairs:
        return None
    vectors = embedder.embed([t for _, t in pairs])
    return {name: v for (name, _), v in zip(pairs, vectors)}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    started = time.time()
    ctx = resolve_pr_context()
    api_url = os.environ.get("ZENIK_API_URL", "")
    dry = os.environ.get("ZENIK_DRY_RUN") == "1"

    # Three modes, one action: a push to the default branch refreshes the
    # stored graph (write path); a pull_request queries it (read path — no
    # upload); a `/zenik fix` PR comment runs the opt-in fix agent.
    event = (os.environ.get("ZENIK_MODE") or
             os.environ.get("GITHUB_EVENT_NAME") or "").strip()
    try:
        if dry:
            # A local dry run needs no GitHub at all: nothing is posted, and
            # without an OIDC token the platform can't be called either, so
            # the blast radius comes from the local parse.
            if api_url and not platform_auth.oidc_available():
                print("[zenik] dry run without a GitHub OIDC token; ignoring "
                      "ZENIK_API_URL and computing the blast radius locally")
                api_url = ""
        elif not platform_auth.oidc_available():
            if ctx.from_fork:
                print("[zenik] this PR comes from a fork; GitHub does not issue "
                      "an OIDC token to fork PRs, so Zenik cannot run on it")
            raise PlatformAuthError(platform_auth.OIDC_MISSING_MESSAGE)

        if event in ("push", "workflow_dispatch", "index"):
            run_index_update(ctx, api_url, started)
            return
        if event in ("issue_comment", "fix"):
            from fix import run_fix
            run_fix()
            return
        run_analysis(ctx, api_url, dry, started)
    except PlatformAuthError as e:
        platform_auth.die(e)


def run_analysis(ctx: PRContext, api_url: str, dry: bool, started: float) -> None:
    """The pull_request path: query the graph, run the findings agent, post."""
    print(f"[zenik] repo={ctx.full_name or '(unknown)'} "
          f"base={ctx.base} head={ctx.head} pr=#{ctx.pr_number}")

    # Decide up front whether this run posts anything, and if so get the app
    # token NOW — an uninstalled app fails the run before any work is spent —
    # and open the check run so the PR shows Zenik is working.
    can_post = bool(not dry and ctx.full_name and ctx.pr_number is not None
                    and ctx.head)
    app: Optional[InstallationToken] = None
    check_id: Optional[int] = None
    if can_post:
        app = InstallationToken.fetch(api_url)
        check_id = create_check_run(ctx.full_name, ctx.head, app.bearer())
    try:
        _analyse_and_post(ctx, api_url, app, check_id, started)
    except PlatformAuthError as e:
        if app is not None and app.token:
            fail_check_run(ctx.full_name, check_id, ctx.head, str(e), app.token)
        raise


def _analyse_and_post(ctx: PRContext, api_url: str,
                      app: Optional[InstallationToken],
                      check_id: Optional[int], started: float) -> None:
    commit_sha = ctx.head or git_head_sha(ctx.repo_path)

    # 1. Parse the checkout (no embedding, no upload — PRs only READ the
    # graph). The local index serves the query vectors and the offline
    # fallback; the stored graph on the platform mirrors the default branch.
    print("[zenik] query mode: parsing checkout (no index upload)")
    index = build_index(str(ctx.repo_path), embed=False,
                        commit_sha=commit_sha, progress=True)

    # 2. Changed symbols from the PR diff.
    changed = changed_symbols(str(ctx.repo_path), base=ctx.base, head=ctx.head)
    print(f"[zenik] changed symbols: {len(changed)}")

    # 3. Blast radius from the platform; local fallback if it is unreachable.
    bundle = None
    if changed:
        qvecs = compute_query_vectors(index, changed)
        resp = post_impact(api_url, ctx.full_name, ctx.pr_number,
                           changed, query_vectors=qvecs)
        if resp is not None:
            bundle = resp
        else:
            print("[zenik] platform impact unavailable (unreachable, or repo "
                  "not yet indexed — merge/run the indexing workflow on the "
                  "default branch). Falling back to a local, deterministic-only "
                  "blast radius.")
            bundle = compute_impact(index, changed, semantic=False).to_dict()
    else:
        print("[zenik] no changed symbols resolved; empty blast radius.")
        bundle = {"changed": [], "impacted": [], "tests": [], "truncated": False}

    impacted = bundle.get("impacted") or []
    tests = bundle.get("tests") or []
    cross = sum(1 for it in impacted if it.get("cross_service"))

    # 5. Findings agent — prose only, no edits.
    diff_text = pr_diff_text(ctx.repo_path, ctx.base, ctx.head)
    agent_result = None
    outcome = telemetry.OUTCOME_NO_IMPACT
    if impacted or tests:
        # The author's stated intent: title + body, with any previous Zenik
        # block stripped so our own counts never read back as "intent".
        intent = "\n\n".join(p for p in (
            ctx.pr_title, strip_description_block(ctx.pr_body)) if p).strip()
        prompt = build_findings_prompt(diff_text, bundle, intent=intent)
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

    # 6. Post the findings as zenik-ai[bot]: inline review comments on the changed
    # hunks first, then the (now shorter) summary comment, the description
    # block, and the check run. Any failure on the review path folds the
    # detail back into the summary — never lose it.
    prose, structured = None, None
    if agent_result is not None and agent_result.ok:
        prose, structured = parse_agent_message(agent_result.final_message or "")
        if structured is None and (agent_result.final_message or "").strip():
            print("[zenik] agent reply had no parseable JSON block; using the "
                  "single-comment layout")

    inline = build_inline_candidates(bundle, structured, diff_text) if impacted else []

    # The agent may have run for most of the token's hour; bearer() re-fetches
    # if it is close to expiry before the posting phase starts.
    token = app.bearer() if app is not None else None
    inline_posted = False
    if inline and token:
        inline_posted = post_pr_review(
            ctx.full_name, ctx.pr_number, ctx.head, inline, token)
    elif inline:
        print(f"[zenik] not posting {len(inline)} inline comment(s) "
              "(dry run / no PR context); bodies follow:\n")
        for c in inline:
            print(f"--- {c['path']}:{c['line']} ---\n{c['body']}")

    body = build_report(
        bundle=bundle, agent_result=agent_result, outcome=outcome,
        pr_number=ctx.pr_number, truncated=bool(bundle.get("truncated")),
        agent_prose=prose, structured=structured, inline_posted=inline_posted,
    )
    publish_findings(ctx, body, token)
    if token:
        update_pr_description(ctx, bundle, structured, token)
        complete_check_run(ctx.full_name, check_id,
                           build_check_run(bundle, outcome, ctx.head), token)

    # 7. Counts-only telemetry.
    duration = round(time.time() - started, 1)
    payload = telemetry.build_payload(
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
    telemetry.send(payload, api_url=api_url)

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
