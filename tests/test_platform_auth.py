"""platform_auth: the OIDC handshake, the fail-loud platform errors, and the
app installation token — all with urllib mocked, no network."""
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

import platform_auth
from platform_auth import (AppNotInstalledError, InstallationToken,
                           PlatformAuthError)

OIDC_URL = "https://pipelines.actions.githubusercontent.com/token?api-version=2.0"


class _Resp:
    """Minimal stand-in for what urlopen returns."""

    def __init__(self, body, status=200):
        self._body = json.dumps(body).encode() if body is not None else b""
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://api.zenik.dev/x", code, "err", {},
        io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", OIDC_URL)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-secret")


@pytest.fixture
def capture(monkeypatch):
    """Record every urlopen call; the queued responses are returned in order
    (an Exception instance is raised instead of returned)."""
    calls = []
    queue = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(platform_auth.urllib.request, "urlopen", fake_urlopen)
    return calls, queue


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------
def test_oidc_token_builds_url_and_headers_and_parses_value(oidc_env, capture):
    calls, queue = capture
    queue.append(_Resp({"value": "eyJ.jwt"}))

    assert platform_auth.oidc_token() == "eyJ.jwt"

    req = calls[0]
    # GitHub's URL already carries a query string: append with `&`.
    assert req.full_url == OIDC_URL + "&audience=zenik-platform"
    assert req.get_method() == "GET"
    assert req.get_header("Authorization") == "bearer runner-secret"


def test_oidc_token_missing_env_fails_with_permission_hint(monkeypatch):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    assert not platform_auth.oidc_available()
    with pytest.raises(PlatformAuthError) as ei:
        platform_auth.oidc_token()
    assert "permissions: id-token: write" in str(ei.value)
    assert "examples/zenik.yml" in str(ei.value)


def test_oidc_token_without_value_fails(oidc_env, capture):
    _, queue = capture
    queue.append(_Resp({"count": 1}))
    with pytest.raises(PlatformAuthError):
        platform_auth.oidc_token()


# ---------------------------------------------------------------------------
# Platform calls
# ---------------------------------------------------------------------------
def test_platform_request_sends_fresh_oidc_bearer(oidc_env, capture):
    calls, queue = capture
    queue += [_Resp({"value": "jwt-1"}), _Resp({"ok": True})]

    out = platform_auth.platform_request(
        "https://api.zenik.dev/", "/v1/impact", {"a": 1}, "POST")

    assert out == {"ok": True}
    api_req = calls[1]
    assert api_req.full_url == "https://api.zenik.dev/v1/impact"
    assert api_req.get_header("Authorization") == "Bearer jwt-1"
    assert json.loads(api_req.data) == {"a": 1}


def test_platform_request_403_app_not_installed_raises_with_install_url(
        oidc_env, capture):
    _, queue = capture
    queue += [_Resp({"value": "jwt"}), _http_error(403, {"detail": {
        "error": "app_not_installed",
        "install_url": "https://github.com/apps/zenik-ai/installations/new",
        "repository": "acme/meridian",
    }})]
    with pytest.raises(AppNotInstalledError) as ei:
        platform_auth.platform_request("https://api.zenik.dev", "/v1/index",
                                       {}, "POST")
    msg = str(ei.value)
    assert "not installed on acme/meridian" in msg
    assert "https://github.com/apps/zenik-ai/installations/new" in msg


def test_platform_request_401_raises(oidc_env, capture):
    _, queue = capture
    queue += [_Resp({"value": "jwt"}),
              _http_error(401, {"detail": "bad audience"})]
    with pytest.raises(PlatformAuthError) as ei:
        platform_auth.platform_request("https://api.zenik.dev", "/v1/impact",
                                       {}, "POST")
    assert "rejected the OIDC token" in str(ei.value)
    assert "bad audience" in str(ei.value)


def test_platform_request_transient_failure_returns_none(oidc_env, capture):
    _, queue = capture
    queue += [_Resp({"value": "jwt"}), _http_error(503, {"detail": "down"})]
    assert platform_auth.platform_request(
        "https://api.zenik.dev", "/v1/impact", {}, "POST") is None


def test_die_prints_error_annotation_and_exits_nonzero(capsys):
    exc = AppNotInstalledError("acme/meridian",
                               "https://github.com/apps/zenik-ai/installations/new")
    with pytest.raises(SystemExit) as ei:
        platform_auth.die(exc)
    assert ei.value.code == 1
    out = capsys.readouterr().out
    assert out.startswith("::error::Zenik GitHub App is not installed on acme/meridian")
    assert "https://github.com/apps/zenik-ai/installations/new" in out


# ---------------------------------------------------------------------------
# Installation token
# ---------------------------------------------------------------------------
def test_installation_token_fetch_posts_without_body_and_parses(oidc_env, capture):
    calls, queue = capture
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    queue += [_Resp({"value": "jwt"}), _Resp({
        "token": "ghs_abc", "expires_at": expires,
        "installation_id": 123, "app_slug": "zenik-ai"})]

    app = InstallationToken.fetch("https://api.zenik.dev")

    req = calls[1]
    assert req.full_url == "https://api.zenik.dev/v1/github/token"
    assert req.get_method() == "POST"
    assert req.data is None
    assert app.token == "ghs_abc"
    assert app.installation_id == 123
    assert app.bot_login == "zenik-ai[bot]"
    assert not app.expiring_soon()
    assert app.bearer() == "ghs_abc"
    assert len(calls) == 2  # no re-fetch while the token is fresh


def test_installation_token_refetches_when_expiring(oidc_env, capture):
    calls, queue = capture
    soon = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    queue += [_Resp({"value": "jwt"}),
              _Resp({"token": "ghs_old", "expires_at": soon}),
              _Resp({"value": "jwt2"}),
              _Resp({"token": "ghs_new", "expires_at": later})]

    app = InstallationToken.fetch("https://api.zenik.dev")
    assert app.expiring_soon()
    assert app.bearer() == "ghs_new"
    assert len(calls) == 4


def test_installation_token_requires_api_url():
    with pytest.raises(PlatformAuthError):
        InstallationToken.fetch("")
