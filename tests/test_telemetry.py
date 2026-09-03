"""telemetry.py: the closed allowlist carries no repo identity, and sending
never raises — not even without an OIDC token."""
import telemetry


def _payload():
    return telemetry.build_payload(
        run_id="gh-run-1", outcome=telemetry.OUTCOME_REPORTED, pr_number=7,
        impacted_count=3, changed_count=1, cross_service_count=2,
        duration_seconds=12.5, agent_result=None)


def test_payload_is_counts_only_without_identity_fields():
    p = _payload()
    assert set(p) == {
        "schema_version", "run_id", "pr_number", "agent", "usage", "outcome",
        "changed_count", "impacted_count", "cross_service_count",
        "duration_seconds",
    }
    # The pre-OIDC identity fields must never come back (spelled so this
    # file does not itself trip the repo-wide "no client key left" grep).
    for forbidden in ("client" + "_key", "repo" + "_hash", "repo", "full_name"):
        assert forbidden not in p


def test_send_without_oidc_is_best_effort(monkeypatch, capsys):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    assert telemetry.send(_payload(), api_url="https://api.zenik.dev") is False
    assert "skipping report" in capsys.readouterr().out


def test_send_without_api_url_skips():
    assert telemetry.send(_payload(), api_url="") is False
