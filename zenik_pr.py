#!/usr/bin/env python3
"""zenik-action orchestrator: PR -> blast radius -> findings -> PR comment.

Follows the prototype's `apply_fix.py` main() flow (load trigger, run agent,
diff stats, write report, telemetry, GITHUB_OUTPUT gating), with two changes:

  * the trigger is the client's own PR rather than a vendor change, and
  * the platform round-trip happens BEFORE the agent, because the blast radius
    is what the agent is given to reason about.

The counts-vs-paths split from the prototype is preserved: paths and symbol
names go into the PR comment the client reads, and only counts go to telemetry.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

if os.environ.get("ZENIK_NO_DOTENV") != "1":
    # override=False: a stray local .env must never beat what the caller's
    # workflow explicitly passed in (the prototype's reasoning, unchanged).
    load_dotenv(Path(__file__).parent / ".env", override=False)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "vendor"))

import github_api  # noqa: E402
import telemetry  # noqa: E402
from agent_backends import AgentResult, select_backend  # noqa: E402
from prompt import build_findings_prompt  # noqa: E402
from report import build_comment, write_report  # noqa: E402

CLIENT_REPO = Path(os.environ.get("CLIENT_REPO_PATH", ".")).resolve()
PLATFORM_URL = os.environ.get("ZENIK_PLATFORM_URL", "").strip()
CLIENT_KEY = os.environ.get("ZENIK_CLIENT_KEY", "").strip()


def _git(*args, cwd=None):
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd or CLIENT_REPO,
            capture_output=True, text=True, timeout=120,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except Exception:
        return ""


def diff_stats() -> dict:
    """Counts and paths, kept separate — only counts reach telemetry."""
    base = os.environ.get("ZENIK_BASE_REF") or github_api.pr_context()["base_ref"]
    merge_base = _git("merge-base", f"origin/{base}", "HEAD").strip() or base
    added = removed = 0
    for line in _git("diff", "--numstat", f"{merge_base}..HEAD").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                added += int(parts[0])
                removed += int(parts[1])
            except ValueError:
                pass  # binary files show as '-'
    files = [f for f in _git("diff", "--name-only", f"{merge_base}..HEAD").splitlines() if f.strip()]
    return {"files": files, "lines_added": added, "lines_removed": removed}


def compute_changed_symbols(base_ref: str):
    from zenik_indexer.diff import changed_symbols

    try:
        syms, snippets = changed_symbols(CLIENT_REPO, f"origin/{base_ref}", "HEAD")
    except RuntimeError:
        # origin/<base> is absent on a shallow checkout; fall back to the plain ref.
        syms, snippets = changed_symbols(CLIENT_REPO, base_ref, "HEAD")
    return syms, snippets


def embed_query(snippets: list[str]) -> list[list[float]]:
    """Embed the changed hunks ON THE CLIENT'S RUNNER.

    The source text never leaves the runner — only the resulting vectors are
    sent to the platform. If no embedding key is configured the semantic half of
    the blast radius is simply skipped; the deterministic edge walk still runs.
    """
    if not snippets:
        return []
    if not os.environ.get("OPENAI_API_KEY"):
        print("[zenik] no OPENAI_API_KEY — semantic layer disabled for this run; "
              "impact will come from import/reference edges only")
        return []
    try:
        from zenik_indexer.embed import Embedder, EmbeddingError

        embedder = Embedder(cache_dir=CLIENT_REPO / ".zenik-cache")
        return embedder.embed(snippets[:20])
    except Exception as exc:  # noqa: BLE001
        print(f"[zenik] embedding failed ({type(exc).__name__}: {exc}); edges only")
        return []


def fetch_impact(repo_full_name, pr_number, head_sha, syms, query_vectors):
    from zenik_indexer.client import PlatformClient, PlatformError

    client = PlatformClient(base_url=PLATFORM_URL or None, client_key=CLIENT_KEY)
    payload = {
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "commit_sha": head_sha,
        "changed_symbols": [
            {"name": s.name, "path": s.path, "kind": s.kind, "language": s.language,
             "start_line": s.start_line, "end_line": s.end_line}
            for s in syms
        ],
        "query_embeddings": query_vectors,
    }
    try:
        return client.impact(payload), None
    except PlatformError as exc:
        return None, str(exc)


def run_agent(prompt_text: str):
    preference = os.environ.get("AGENT_BACKEND", "").strip() or None
    model = os.environ.get("AGENT_MODEL", "").strip() or None
    try:
        timeout = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "") or 900)
    except ValueError:
        timeout = 900

    backend = select_backend(preference=preference, timeout=timeout, model=model)
    if backend is None:
        print("[zenik] no agent key configured (OPENAI_API_KEY or ANTHROPIC_API_KEY); "
              "posting the computed impact list without prose guidance")
        return None

    print(f"[zenik] using agent backend: {backend.name}")
    if not backend.ensure_installed():
        return AgentResult(backend=backend.name, ok=False,
                           error=f"failed to install {backend.package}")

    started = time.time()
    result = backend.run(prompt_text, workdir=str(CLIENT_REPO))
    result.duration_seconds = round(time.time() - started, 1)
    if not result.ok:
        print(f"[zenik] agent did not complete: {result.error}")
    return result


# Written by this action itself; never counted as an agent edit.
OWN_ARTIFACTS = {"zenik_findings.md"}


def tree_state() -> dict[str, str]:
    """{path: status} from `git status --porcelain`, excluding our own output."""
    state: dict[str, str] = {}
    for line in _git("status", "--porcelain").splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:].strip().strip('"')
        if path in OWN_ARTIFACTS:
            continue
        state[path] = status
    return state


def enforce_read_only(before: dict[str, str]) -> int:
    """Undo anything the AGENT wrote, and nothing else.

    v0 is guidance-only and the PR comment says so; an agent that edits the
    checkout would make that claim false. But this runs inside a client's
    working tree, so the revert has to be surgical:

      * only paths that changed BETWEEN the pre-agent snapshot and now are
        touched — a file the developer already had dirty is left alone;
      * new files the agent created are deleted individually by path. There is
        deliberately no `git clean -fd` here: that would also delete untracked
        files belonging to the developer (local config, .env, build output).
    """
    after = tree_state()
    changed = [p for p, st in after.items() if before.get(p) != st]
    if not changed:
        return 0

    print(f"[zenik] WARNING: the agent modified {len(changed)} path(s) during a "
          f"read-only review. Reverting those paths; v0 does not produce code changes.")
    for path in changed[:20]:
        print(f"  {after[path]} {path}")

    tracked = [p for p in changed if not after[p].startswith("??")]
    untracked = [p for p in changed if after[p].startswith("??")]
    if tracked:
        _git("checkout", "--", *tracked)
    for path in untracked:
        try:
            target = (CLIENT_REPO / path).resolve()
            # Refuse to delete anything that resolved outside the checkout.
            target.relative_to(CLIENT_REPO)
            if target.is_file():
                target.unlink()
        except (ValueError, OSError) as exc:
            print(f"[zenik] could not remove {path}: {exc}")
    return len(changed)


def main() -> int:
    started = time.time()
    ctx = github_api.pr_context()
    repo_full_name = ctx["repo"] or telemetry.env_repo_full_name() or "unknown/unknown"
    token = os.environ.get("GITHUB_TOKEN", "")

    if not CLIENT_KEY:
        print("[zenik] ZENIK_CLIENT_KEY is not set; cannot reach the platform.")
        return 2

    print(f"[zenik] repo={repo_full_name} pr={ctx['pr_number']} base={ctx['base_ref']}")

    syms, snippets = compute_changed_symbols(ctx["base_ref"])
    print(f"[zenik] {len(syms)} changed symbol(s) from the diff")
    for s in syms[:15]:
        print(f"  {s.path}:{s.start_line} {s.name} [{s.kind}]")

    stats = diff_stats()
    agent_result = None
    bundle = {"impact": [], "tests": []}
    impact_stats: dict = {}

    if not syms:
        outcome = telemetry.OUTCOME_NO_SYMBOLS
        comment = None
    else:
        query_vectors = embed_query(snippets)
        bundle_resp, error = fetch_impact(
            repo_full_name, ctx["pr_number"], ctx["head_sha"], syms, query_vectors
        )
        if error:
            print(f"[zenik] platform unreachable: {error}")
            outcome = telemetry.OUTCOME_PLATFORM_FAILED
            comment = None
        else:
            bundle = bundle_resp
            impact_stats = bundle.get("stats") or {}
            print(f"[zenik] blast radius: {len(bundle.get('impact') or [])} site(s), "
                  f"{len(bundle.get('tests') or [])} test(s)")

            agent_markdown = ""
            if bundle.get("impact"):
                prompt_text = build_findings_prompt(
                    diff=_git("diff", f"origin/{ctx['base_ref']}...HEAD") or _git("diff", "HEAD~1"),
                    changed_symbols=[
                        {"name": s.name, "path": s.path, "kind": s.kind,
                         "start_line": s.start_line, "end_line": s.end_line}
                        for s in syms
                    ],
                    bundle=bundle,
                    repo_full_name=repo_full_name,
                    pr_number=ctx["pr_number"],
                )
                before_agent = tree_state()
                agent_result = run_agent(prompt_text)
                if agent_result is not None:
                    enforce_read_only(before_agent)
                if agent_result is not None and agent_result.ok:
                    agent_markdown = agent_result.final_message or ""
                    if not agent_markdown.strip():
                        # The prototype's hard-won lesson: a successful run that
                        # produced nothing is ambiguous, not clean. Say so.
                        print("[zenik] NOTE: the agent completed but returned no "
                              "guidance. That is a silent no-op, not a clean pass.")

            if agent_result is not None and not agent_result.ok:
                outcome = telemetry.OUTCOME_AGENT_FAILED
            elif bundle.get("impact"):
                outcome = telemetry.OUTCOME_REPORTED
            else:
                outcome = telemetry.OUTCOME_NO_IMPACT

            comment = build_comment(
                changed_symbols=[{"name": s.name, "path": s.path} for s in syms],
                bundle=bundle,
                agent_markdown=agent_markdown,
                outcome=outcome,
                stats=impact_stats,
                run_url=ctx["run_url"],
            )

    if comment:
        write_report(comment, CLIENT_REPO / "zenik_findings.md")
        if ctx["pr_number"]:
            github_api.upsert_pr_comment(repo_full_name, ctx["pr_number"], comment, token)
            github_api.create_check(
                repo_full_name, ctx["head_sha"], token,
                title=f"{len(bundle.get('impact') or [])} affected site(s)",
                summary=comment,
            )
        else:
            print("[zenik] no PR number in context; wrote zenik_findings.md only")

    payload = telemetry.build_payload(
        client_key=CLIENT_KEY,
        repo_full_name=repo_full_name,
        run_id=telemetry.env_run_id(),
        outcome=outcome,
        files_changed_count=len(stats["files"]),
        lines_added=stats["lines_added"],
        lines_removed=stats["lines_removed"],
        pr_expected=False,  # v0 never opens a PR — guidance only
        duration_seconds=round(time.time() - started, 1),
        agent_result=agent_result,
    )
    telemetry.print_payload(payload)
    telemetry.send(payload, api_url=PLATFORM_URL, client_key=CLIENT_KEY)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as fh:
            fh.write(f"outcome={outcome}\n")
            fh.write(f"affected-sites={len(bundle.get('impact') or [])}\n")
            fh.write(f"cross-service={impact_stats.get('cross_language_hits', 0)}\n")

    print(f"[zenik] done. outcome={outcome}")
    if outcome == telemetry.OUTCOME_AGENT_FAILED:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
