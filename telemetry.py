#!/usr/bin/env python3
"""Telemetry — what this action reports back to Zenik, and what it never does.

Forked from the prior prototype's telemetry.py and re-pointed at the Zenik
platform's `/v1/telemetry`. The trust model in code is unchanged — only the
payload fields are adapted from a "vendor-change fix" run to a "PR blast-radius"
run.

This is the ONE place where data flows from a client's runner back to us, so
the rules are enforced here in code rather than promised in a doc:

  1. The payload is a CLOSED ALLOWLIST. build_payload() constructs a fixed set
     of keys. There is no passthrough, no **kwargs, no "extra" dict. Adding a
     field is a deliberate edit to this file, reviewable in a diff.

  2. Nothing that describes the client's code is included. Not file paths, not
     file names, not symbol names, not the diff, not source snippets, not
     branch or repo names, not the agent's prose findings. COUNTS AND ENUMS
     ONLY — the impact set is reported as how-many, never which.

  3. Repo identity is a SALTED HASH, never the name. It is stable, so a
     dashboard can count distinct repos and track one over time, but it is not
     reversible to a name. The salt is the client key, so hashes are not even
     comparable across clients. (The platform's `repos.full_name` holds the
     plaintext name for the dashboard — but that arrives via /v1/index, a
     different, per-client-isolated path. This telemetry path must never learn
     names; see BUILD.md §10.4.)

  4. Sending NEVER blocks or fails the client's build. Every failure path here
     is swallowed. If our endpoint is down, the client still gets their PR
     comment. Our metrics are not worth breaking someone's CI.

What we do send is deliberately boring: did we report an impact, how big was
the blast radius, how many crossed a service boundary, how many tokens the
agent used. That is what a usage/accuracy dashboard needs and it reveals
nothing about what the client's software does.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

SCHEMA_VERSION = 2  # bumped from the prototype's 1: PR-run field set, not fix-run.

# Short by design. This call is a side effect, not part of the job: if it
# cannot complete quickly we drop it rather than delay the client's run.
TELEMETRY_TIMEOUT_SECONDS = 5

# Outcome enum — the single field that answers "what did this run do?"
OUTCOME_REPORTED = "reported"          # findings computed and posted to the PR
OUTCOME_NO_IMPACT = "no_impact"        # ran, blast radius was empty; nothing to report
OUTCOME_AGENT_FAILED = "agent_failed"  # agent errored or timed out
OUTCOME_INDEXED = "indexed"            # index-update run (push): graph refreshed


def hash_repo(repo_full_name, client_key):
    """Stable, non-reversible repo identifier.

    Salted with the client key so the same repo under two different clients
    hashes differently, and so a hash cannot be checked against a rainbow
    table of common `owner/name` strings. Identical to the prototype's
    implementation so `telemetry.repo_hash` lines up with `repos.repo_hash`.
    """
    if not repo_full_name:
        return None
    salt = client_key or "unsalted"
    digest = hashlib.sha256(f"{salt}:{repo_full_name}".encode()).hexdigest()
    return f"sha256:{digest[:32]}"


def build_payload(
    *,
    client_key,
    repo_full_name,
    run_id,
    outcome,
    pr_number,
    impacted_count,
    changed_count,
    cross_service_count,
    duration_seconds,
    agent_result=None,
):
    """Construct the complete telemetry payload.

    Every key in the returned dict is written literally below. If a value isn't
    here, it isn't sent. Note the deliberate ABSENCE of anything naming code:
    the impact set is three integers (changed / impacted / cross-service), never
    the symbols themselves.
    """
    agent_block = {
        "backend": None,
        "model": None,
        "cli_version": None,
    }
    usage_block = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "cost_usd": None,
    }

    if agent_result is not None:
        agent_block["backend"] = agent_result.backend
        agent_block["model"] = agent_result.model
        agent_block["cli_version"] = agent_result.cli_version
        usage_block = agent_result.usage.as_dict()

    return {
        "schema_version": SCHEMA_VERSION,

        # Who and when.
        "client_key": client_key,
        "repo_hash": hash_repo(repo_full_name, client_key),
        "run_id": run_id,
        "pr_number": pr_number,

        # What the agent was and what it cost.
        "agent": agent_block,
        "usage": usage_block,

        # What happened — COUNTS ONLY, never names.
        "outcome": outcome,
        "changed_count": changed_count,
        "impacted_count": impacted_count,
        "cross_service_count": cross_service_count,
        "duration_seconds": duration_seconds,
    }


def send(payload, api_url, client_key):
    """POST the payload. Returns True on success, False otherwise — never raises.

    Telemetry is best-effort by design. Every exception below is caught and
    logged, because the alternative is our reporting endpoint being able to
    fail a client's CI run.
    """
    if not api_url:
        print("[telemetry] no zenik-api-url configured; skipping report")
        return False

    url = api_url.rstrip("/") + "/v1/telemetry"
    body = json.dumps(payload).encode()

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client_key}",
            "User-Agent": "zenik-action/1",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TELEMETRY_TIMEOUT_SECONDS) as resp:
            if 200 <= resp.status < 300:
                print(f"[telemetry] reported run outcome={payload['outcome']} "
                      f"impacted={payload['impacted_count']}")
                return True
            print(f"[telemetry] endpoint returned HTTP {resp.status}; ignoring")
            return False
    except urllib.error.HTTPError as e:
        print(f"[telemetry] HTTP {e.code} from endpoint; ignoring")
    except urllib.error.URLError as e:
        print(f"[telemetry] could not reach endpoint ({e.reason}); ignoring")
    except Exception as e:  # noqa: BLE001 - never let telemetry break the build
        print(f"[telemetry] unexpected error ({type(e).__name__}); ignoring")
    return False


def print_payload(payload):
    """Echo exactly what we're about to send, into the client's own CI log.

    Deliberate transparency: a client can read their workflow log and see the
    complete telemetry body, rather than taking our word for it.
    """
    print("[telemetry] payload (this is everything that is sent):")
    for line in json.dumps(payload, indent=2).splitlines():
        print(f"  {line}")


def env_repo_full_name():
    """GitHub sets GITHUB_REPOSITORY to 'owner/name' on every runner."""
    return os.environ.get("GITHUB_REPOSITORY")


def env_run_id():
    run_id = os.environ.get("GITHUB_RUN_ID")
    return f"gh-run-{run_id}" if run_id else None
