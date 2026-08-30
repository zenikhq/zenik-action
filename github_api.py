#!/usr/bin/env python3
"""Posting back to the PR — through the CLIENT's own `GITHUB_TOKEN`.

There is no Zenik GitHub identity anywhere in this file, by design (BUILD.md
§10.1). Everything posted here is authored by the client's own
`github-actions[bot]`, in the client's own repo, using a token their workflow
granted. Zenik never holds a GitHub credential for a client repository.

Failures here are logged and swallowed: a comment we could not post is not worth
failing someone's build over.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from report import MARKER

API = "https://api.github.com"
TIMEOUT = 20


def _request(method: str, url: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "zenik-action/1",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read() or b"{}")


def upsert_pr_comment(repo: str, pr_number: int, body: str, token: str) -> bool:
    """Update Zenik's existing comment on this PR, or create one.

    Updating matters: a PR gets pushed to repeatedly, and a new impact comment
    per push buries the conversation. The marker in report.py is how the
    previous one is found.
    """
    if not token:
        print("[github] no GITHUB_TOKEN available; skipping PR comment")
        return False
    try:
        existing = _request(
            "GET", f"{API}/repos/{repo}/issues/{pr_number}/comments?per_page=100", token
        )
        mine = next((c for c in existing if MARKER in (c.get("body") or "")), None)
        if mine:
            _request("PATCH", f"{API}/repos/{repo}/issues/comments/{mine['id']}",
                     token, {"body": body})
            print(f"[github] updated existing comment {mine['id']}")
        else:
            _request("POST", f"{API}/repos/{repo}/issues/{pr_number}/comments",
                     token, {"body": body})
            print(f"[github] posted comment on PR #{pr_number}")
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        print(f"[github] could not post comment (HTTP {exc.code}): {detail}")
    except Exception as exc:  # noqa: BLE001
        print(f"[github] could not post comment ({type(exc).__name__}: {exc})")
    return False


def create_check(repo: str, sha: str, token: str, *, title: str, summary: str,
                 conclusion: str = "neutral") -> bool:
    """A check run alongside the comment, so the impact shows in the PR's checks.

    `neutral` rather than `success`/`failure` on purpose: v0 reports impact, it
    does not judge whether the PR is acceptable. Turning this into a gate is a
    product decision nobody has made yet.
    """
    if not token:
        return False
    try:
        _request("POST", f"{API}/repos/{repo}/check-runs", token, {
            "name": "Zenik — change impact",
            "head_sha": sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary[:65000]},
        })
        print("[github] created check run")
        return True
    except urllib.error.HTTPError as exc:
        # 403 here is normal and expected when the workflow did not grant
        # `checks: write`; the comment is the primary surface anyway.
        print(f"[github] check run not created (HTTP {exc.code}) — needs "
              f"`permissions: checks: write` in the client workflow")
    except Exception as exc:  # noqa: BLE001
        print(f"[github] check run not created ({type(exc).__name__})")
    return False


def pr_context() -> dict:
    """Read PR number, base ref and head SHA from the Actions event payload."""
    ctx = {
        "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "pr_number": None,
        "base_ref": os.environ.get("GITHUB_BASE_REF", "") or "main",
        "head_sha": os.environ.get("GITHUB_SHA", ""),
        "run_url": None,
    }
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if ctx["repo"] and run_id:
        ctx["run_url"] = f"{server}/{ctx['repo']}/actions/runs/{run_id}"

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path) as fh:
                event = json.load(fh)
            pr = event.get("pull_request") or {}
            if pr:
                ctx["pr_number"] = pr.get("number")
                ctx["base_ref"] = (pr.get("base") or {}).get("ref") or ctx["base_ref"]
                ctx["head_sha"] = (pr.get("head") or {}).get("sha") or ctx["head_sha"]
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[github] could not read event payload: {exc}")

    # Allow a local run to stand in for the event payload.
    if ctx["pr_number"] is None and os.environ.get("ZENIK_PR_NUMBER"):
        try:
            ctx["pr_number"] = int(os.environ["ZENIK_PR_NUMBER"])
        except ValueError:
            pass
    return ctx
