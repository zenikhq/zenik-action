#!/usr/bin/env python3
"""The `/zenik fix` opt-in fix agent (BUILD.md §0.6.3).

Runs when a person with write access comments `/zenik fix` on a PR. This is
the moment Zenik graduates from describing the blast radius to WRITING code in
the client's repo — so the shape of this file is gates first, work second:

  1. Parse the command (`/zenik fix [symbol ...]`); anything unrecognized gets
     a usage reply, never a guess.
  2. Permission gate: the commenter's author_association must be OWNER/MEMBER/
     COLLABORATOR. Anyone can comment on a PR; only maintainers command a
     code-writing agent that runs with repo secrets.
  3. Fork gate: the PR head must live in THIS repo. A fix agent never runs on
     an outside contributor's branch.
  4. Ack immediately ("on it") — the Actions tab is invisible; silence reads
     as breakage.

Then the run: check out the PR head branch, recompute the blast radius fresh
(the findings comments may predate newer commits), hand the agent a fix prompt
scoped to the implicated files, ENFORCE that scope in code by reverting any
out-of-scope edits, commit crediting the requester, push, and reply with what
changed. Pushes made with the default GITHUB_TOKEN do not re-trigger workflows
(GitHub's loop prevention), so the findings run won't re-fire on the fix
commit — the human reviews the diff; that is the contract.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent / "vendor"))
sys.path.insert(0, str(Path(__file__).parent))

import telemetry  # noqa: E402

_ALLOWED_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}

USAGE = (
    "Usage: `/zenik fix` applies every finding from the latest Zenik analysis; "
    "`/zenik fix <symbol> [...]` scopes it to those changed symbols."
)


# ---------------------------------------------------------------------------
# Event + command parsing
# ---------------------------------------------------------------------------
def load_event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def parse_command(body: str) -> Optional[dict]:
    """`/zenik fix [symbol ...]` -> {"scope": [...]}; other /zenik input ->
    {"error": usage}; non-command comments -> None (ignore silently)."""
    words = (body or "").strip().split()
    if not words or words[0].lower() != "/zenik":
        return None
    if len(words) >= 2 and words[1].lower() == "fix":
        return {"scope": words[2:]}
    return {"error": USAGE}


# ---------------------------------------------------------------------------
# Git plumbing (the workflow checked out the DEFAULT branch — issue_comment
# events do — so this run switches to the PR head branch itself).
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=timeout)


def checkout_pr_branch(repo: Path, head_ref: str) -> bool:
    fetch = _git(repo, "fetch", "origin", head_ref)
    if fetch.returncode != 0:
        print(f"[zenik-fix] fetch failed: {fetch.stderr.strip()[:300]}")
        return False
    co = _git(repo, "checkout", "-B", head_ref, f"origin/{head_ref}")
    if co.returncode != 0:
        print(f"[zenik-fix] checkout failed: {co.stderr.strip()[:300]}")
        return False
    return True


def changed_files(repo: Path) -> list[str]:
    out = _git(repo, "status", "--porcelain")
    files = []
    for line in out.stdout.splitlines():
        if len(line) > 3:
            # rename entries look like "R  old -> new"; keep the new path
            path = line[3:].split(" -> ")[-1].strip()
            files.append(path)
    return files


def revert_paths(repo: Path, paths: list[str]) -> None:
    for p in paths:
        _git(repo, "checkout", "--", p)
        # a newly created out-of-scope file has nothing to check out; remove it
        f = repo / p
        if f.exists() and _git(repo, "ls-files", "--error-unmatch", p).returncode != 0:
            try:
                f.unlink()
            except OSError:
                pass


def commit_and_push(repo: Path, head_ref: str, symbols: list[str],
                    requester: str) -> bool:
    _git(repo, "config", "user.name", "github-actions[bot]")
    _git(repo, "config", "user.email",
         "41898282+github-actions[bot]@users.noreply.github.com")
    _git(repo, "add", "-A")
    scope = ", ".join(symbols) if symbols else "the reported findings"
    msg = (f"Zenik: apply fixes for {scope}\n\n"
           f"Requested by @{requester} via /zenik fix. Review before merging.")
    commit = _git(repo, "commit", "-m", msg)
    if commit.returncode != 0:
        print(f"[zenik-fix] commit failed: {commit.stderr.strip()[:300]}")
        return False
    push = _git(repo, "push", "origin", head_ref, timeout=300)
    if push.returncode != 0:
        print(f"[zenik-fix] push failed: {push.stderr.strip()[:300]}")
        return False
    return True


# ---------------------------------------------------------------------------
# The fix run
# ---------------------------------------------------------------------------
def run_fix() -> None:
    # Imported here (not at module top) so run_zenik can `import fix` without a
    # circular import at load time.
    from run_zenik import (_gh_request, _github_api, CLIENT_REPO,
                           compute_query_vectors, post_impact, pr_diff_text,
                           run_agent)
    from zenik_indexer import build_index, changed_symbols, compute_impact
    from prompt import build_fix_prompt
    from report import COMMENT_MARKER, INLINE_MARKER

    started = time.time()
    event = load_event()
    comment = event.get("comment") or {}
    issue = event.get("issue") or {}
    full_name = (event.get("repository") or {}).get("full_name") or ""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    api = _github_api()
    client_key = os.environ.get("ZENIK_CLIENT_KEY", "")
    api_url = os.environ.get("ZENIK_API_URL", "")

    cmd = parse_command(comment.get("body") or "")
    if cmd is None:
        print("[zenik-fix] not a /zenik command; nothing to do")
        return
    pr_number = issue.get("number")
    if not issue.get("pull_request") or pr_number is None:
        print("[zenik-fix] /zenik on a plain issue; only PRs are supported")
        return

    def reply(text: str) -> None:
        if token and full_name:
            _gh_request("POST",
                        f"{api}/repos/{full_name}/issues/{pr_number}/comments",
                        token, {"body": text})
        else:
            print(f"[zenik-fix] (no token) reply would be:\n{text}")

    if "error" in cmd:
        reply(cmd["error"])
        return

    requester = (comment.get("user") or {}).get("login") or "unknown"
    assoc = comment.get("author_association") or ""
    if assoc not in _ALLOWED_ASSOC:
        print(f"[zenik-fix] {requester} has association {assoc}; declining")
        reply(f"@{requester} `/zenik fix` needs write access to this "
              f"repository, and your association is `{assoc or 'NONE'}`. "
              f"Ask a maintainer to run it.")
        return

    pr = _gh_request("GET", f"{api}/repos/{full_name}/pulls/{pr_number}", token)
    if not pr:
        reply("Could not load this PR from the GitHub API; try again.")
        return
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo = (head.get("repo") or {}).get("full_name") or ""
    if head_repo != full_name:
        print(f"[zenik-fix] fork PR ({head_repo}); declining")
        reply("`/zenik fix` only runs on branches in this repository — this "
              "PR's branch lives in a fork, and Zenik won't run a code-writing "
              "agent with repo secrets on a fork's code. Apply the findings "
              "manually, or push the branch to this repo.")
        return

    scope = cmd.get("scope") or []
    scope_note = f" for `{'`, `'.join(scope)}`" if scope else ""
    reply(f"⏳ On it — applying Zenik's findings{scope_note}. "
          f"I'll push to `{head.get('ref')}` and report back here.")

    if not checkout_pr_branch(CLIENT_REPO, head.get("ref") or ""):
        reply("Could not check out the PR branch on the runner; "
              "see the workflow log.")
        sys.exit(1)

    # Recompute the blast radius FRESH — the findings comments may predate
    # newer commits on the branch. Same read-only query path as a PR run.
    base_sha, head_sha = base.get("sha"), head.get("sha")
    diff_text = pr_diff_text(CLIENT_REPO, base_sha, head_sha)
    index = build_index(str(CLIENT_REPO), embed=False, commit_sha=head_sha,
                        progress=True)
    changed = changed_symbols(str(CLIENT_REPO), base=base_sha, head=head_sha)
    if scope:
        wanted = set(scope)
        changed = [c for c in changed if c.name in wanted]
        if not changed:
            reply(f"None of {scope_note.strip() or 'those symbols'} match the "
                  f"changed symbols on this PR — nothing to fix. {USAGE}")
            return
    bundle = None
    if changed:
        qvecs = compute_query_vectors(index, changed)
        bundle = post_impact(api_url, client_key, full_name, pr_number,
                             changed, query_vectors=qvecs)
    if bundle is None:
        bundle = (compute_impact(index, changed, semantic=False).to_dict()
                  if changed else
                  {"changed": [], "impacted": [], "tests": [], "truncated": False})

    # Prior Zenik findings (summary + inline notes) travel as context so the
    # agent fixes what was REPORTED, not what it improvises.
    findings: list[str] = []
    listed = _gh_request(
        "GET", f"{api}/repos/{full_name}/issues/{pr_number}/comments?per_page=100",
        token)
    for c in (listed if isinstance(listed, list) else []):
        if COMMENT_MARKER in (c.get("body") or ""):
            findings.append(c["body"])
    inline = _gh_request(
        "GET", f"{api}/repos/{full_name}/pulls/{pr_number}/comments?per_page=100",
        token)
    for c in (inline if isinstance(inline, list) else []):
        if INLINE_MARKER in (c.get("body") or ""):
            findings.append(f"(inline on {c.get('path')}:{c.get('line')})\n"
                            f"{c.get('body')}")

    prompt = build_fix_prompt(diff_text, bundle, findings, scope)
    agent_result = run_agent(prompt)
    if agent_result is None or not agent_result.ok:
        err = (agent_result.error if agent_result else
               "no agent key configured (OPENAI_API_KEY / ANTHROPIC_API_KEY)")
        reply(f"⚠️ The fix agent did not complete: {str(err)[:400]}\n\n"
              f"No changes were pushed.")
        _send_fix_telemetry(client_key, full_name, pr_number, "fix_failed",
                            agent_result, started, api_url)
        sys.exit(1)

    # Scope enforcement in code, not just prompt: the agent may only touch the
    # implicated files (the PR's own changed files + impacted + flagged tests).
    allowed = {c.get("path") for c in bundle.get("changed") or []}
    for it in (bundle.get("impacted") or []) + (bundle.get("tests") or []):
        allowed.add((it.get("symbol") or {}).get("path"))
    allowed.discard(None)
    edited = changed_files(CLIENT_REPO)
    out_of_scope = [p for p in edited
                    if p not in allowed or p.startswith(".github/")]
    if out_of_scope:
        print(f"[zenik-fix] reverting out-of-scope edits: {out_of_scope}")
        revert_paths(CLIENT_REPO, out_of_scope)
        edited = changed_files(CLIENT_REPO)

    if not edited:
        note = ("I reverted them because they were outside the reported "
                "blast radius." if out_of_scope else "")
        reply("The agent reviewed the findings and made no code changes. "
              + (f"(It attempted edits to {len(out_of_scope)} out-of-scope "
                 f"file(s) — {note})" if out_of_scope else
                 "Its summary:\n\n" +
                 (agent_result.final_message or "(none)")[:2000]))
        _send_fix_telemetry(client_key, full_name, pr_number, "fixed",
                            agent_result, started, api_url)
        return

    if not commit_and_push(CLIENT_REPO, head.get("ref") or "",
                           [c.name for c in changed], requester):
        reply("⚠️ The fixes were written but the commit/push failed — "
              "see the workflow log. No changes reached the branch.")
        _send_fix_telemetry(client_key, full_name, pr_number, "fix_failed",
                            agent_result, started, api_url)
        sys.exit(1)

    stat = _git(CLIENT_REPO, "show", "--stat", "--format=", "HEAD").stdout.strip()
    dropped = (f"\n\n_Reverted {len(out_of_scope)} out-of-scope edit(s): "
               + ", ".join(f"`{p}`" for p in out_of_scope) + "_"
               if out_of_scope else "")
    summary = (agent_result.final_message or "").strip()[:2500]
    reply(f"✅ Pushed fixes to `{head.get('ref')}` — **review the diff before "
          f"merging.**\n\n```\n{stat}\n```\n\n{summary}{dropped}\n\n"
          f"_Note: this push does not re-trigger CI analysis "
          f"(GitHub token loop prevention) — re-run the Zenik workflow to "
          f"re-check the blast radius if needed._")
    _send_fix_telemetry(client_key, full_name, pr_number, "fixed",
                        agent_result, started, api_url)
    print(f"[zenik-fix] Done. files_changed={len(edited)}")


def _send_fix_telemetry(client_key, full_name, pr_number, outcome,
                        agent_result, started, api_url) -> None:
    payload = telemetry.build_payload(
        client_key=client_key, repo_full_name=full_name,
        run_id=telemetry.env_run_id(), outcome=outcome, pr_number=pr_number,
        impacted_count=0, changed_count=0, cross_service_count=0,
        duration_seconds=round(time.time() - started, 1),
        agent_result=agent_result,
    )
    telemetry.print_payload(payload)
    telemetry.send(payload, api_url=api_url, client_key=client_key)


if __name__ == "__main__":
    run_fix()
