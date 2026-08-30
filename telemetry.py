#!/usr/bin/env python3
"""Telemetry — what this action reports back to Zenik, and what it never does.

Carried over from the prior prototype with the payload adapted to Zenik's PR
trigger. The rules are unchanged and are enforced here in code rather than
promised in a doc:

  1. The payload is a CLOSED ALLOWLIST. build_payload() constructs a fixed set
     of keys. No passthrough, no **kwargs, no "extra" dict. Adding a field is a
     deliberate edit to this file, reviewable in a diff.

  2. Nothing describing the client's code is included. Not file paths, not
     symbol names, not the diff, not the blast-radius result, not the agent's
     prose. Counts and enums only. This is why the blast radius — which is full
     of real paths and names — travels on the *other* API (/v1/impact) and never
     touches this one.

  3. Repo identity is a SALTED HASH, never the name. Stable, so a dashboard can
     count distinct repos over time; not reversible to a name. The salt is the
     client key, so hashes are not comparable across clients.

  4. Sending NEVER blocks or fails the client's build. Every failure path is
     swallowed. Our metrics are not worth breaking someone's CI.

CHANGED FROM THE PROTOTYPE: the vendor-change fields (vendor/api/old_version/
new_version/change_ids) are gone — the trigger is now the client's own PR, and
there is no vendor. The agent/usage blocks are flattened to match the platform's
`telemetry` table columns.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

SCHEMA_VERSION = 1

# Short by design. This call is a side effect, not part of the job.
TELEMETRY_TIMEOUT_SECONDS = 5

# Outcome enum — the single field that answers "did this run do anything?"
OUTCOME_REPORTED = "findings_reported"     # blast radius computed + posted
OUTCOME_NO_IMPACT = "no_impact_found"      # ran, nothing downstream affected
OUTCOME_NO_SYMBOLS = "no_symbols_changed"  # diff touched nothing we could parse
OUTCOME_AGENT_FAILED = "agent_failed"      # agent errored or timed out
OUTCOME_PLATFORM_FAILED = "platform_unreachable"  # impact API unavailable


def hash_repo(repo_full_name, client_key):
    """Stable, non-reversible repo identifier.

    Salted with the client key so the same repo under two different clients
    hashes differently, and so a hash cannot be checked against a rainbow table
    of common `owner/name` strings.
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
    files_changed_count,
    lines_added,
    lines_removed,
    pr_expected,
    duration_seconds,
    agent_result=None,
):
    """Construct the complete telemetry payload.

    Every key in the returned dict is written literally below. If a value isn't
    here, it isn't sent.
    """
    backend = model = None
    input_tokens = output_tokens = cost_usd = None
    if agent_result is not None:
        backend = agent_result.backend
        model = agent_result.model
        usage = agent_result.usage.as_dict()
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cost_usd = usage.get("cost_usd")

    return {
        "schema_version": SCHEMA_VERSION,
        "repo_hash": hash_repo(repo_full_name, client_key),
        "run_id": run_id,
        "outcome": outcome,
        "files_changed_count": files_changed_count,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "pr_expected": pr_expected,
        "duration_seconds": duration_seconds,
        "agent_backend": backend,
        "agent_model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }


def send(payload, api_url, client_key):
    """POST the payload. Returns True on success — never raises.

    Telemetry is best-effort by design. Every exception below is caught and
    logged, because the alternative is our reporting endpoint being able to fail
    a client's CI run.
    """
    if not api_url:
        print("[telemetry] no zenik-platform-url configured; skipping report")
        return False

    url = api_url.rstrip("/") + "/v1/telemetry"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client_key}",
            "User-Agent": "zenik-action/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TELEMETRY_TIMEOUT_SECONDS) as resp:
            if 200 <= resp.status < 300:
                print(f"[telemetry] reported outcome={payload['outcome']}")
                return True
            print(f"[telemetry] endpoint returned HTTP {resp.status}; ignoring")
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
    complete telemetry body rather than taking our word for it.
    """
    print("[telemetry] payload (this is everything that is sent):")
    for line in json.dumps(payload, indent=2).splitlines():
        print(f"  {line}")


def env_repo_full_name():
    return os.environ.get("GITHUB_REPOSITORY")


def env_run_id():
    run_id = os.environ.get("GITHUB_RUN_ID")
    return f"gh-run-{run_id}" if run_id else None
