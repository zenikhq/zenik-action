#!/usr/bin/env python3
"""How this action proves who it is — to the Zenik platform and to GitHub.

There is ONE auth path, and it holds no Zenik secret:

  1. **GitHub Actions OIDC → the platform.** When the client's workflow grants
     `permissions: id-token: write`, GitHub injects ACTIONS_ID_TOKEN_REQUEST_URL
     and ACTIONS_ID_TOKEN_REQUEST_TOKEN into the job. `oidc_token()` exchanges
     those for a short-lived JWT whose `repository_id` claim the platform uses
     to resolve the repo. Every platform call (`/v1/index`, `/v1/impact`,
     `/v1/telemetry`, `/v1/github/token`, ...) sends it as `Bearer`. A FRESH
     token is requested per call: they are short-lived and an index upload can
     take minutes.

  2. **The Zenik GitHub App → GitHub.** The platform holds the app's private
     key; `InstallationToken` asks it (`POST /v1/github/token`, OIDC-authed)
     for a one-hour installation token scoped to this repo (pull_requests +
     checks write, metadata read — deliberately NO contents access). Every
     GitHub write the action makes goes through that token, so everything
     appears as `zenik-ai[bot]`. There is no fallback to the workflow's
     GITHUB_TOKEN: if the app isn't installed, the run fails loudly with the
     install link.

Failures that mean "this repo isn't set up" raise `PlatformAuthError` (or its
`AppNotInstalledError` subclass); `die()` turns one into a `::error::`
annotation and a non-zero exit. Transient platform failures (unreachable,
5xx) are still reported as `None` by `platform_request`, so the callers keep
their graceful local fallbacks.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

OIDC_AUDIENCE = "zenik-platform"
INSTALL_URL = "https://github.com/apps/zenik-ai/installations/new"

OIDC_MISSING_MESSAGE = (
    "Zenik needs `permissions: id-token: write` in the workflow "
    "(see examples/zenik.yml)")

_PLATFORM_TIMEOUT = 120
_OIDC_TIMEOUT = 30

# Installation tokens live 1h. Re-fetch when fewer than this many seconds
# remain — an agent run can take most of an hour.
_REFRESH_MARGIN_SECONDS = 5 * 60


class PlatformAuthError(Exception):
    """This run cannot authenticate; the workflow must fail visibly."""


class AppNotInstalledError(PlatformAuthError):
    def __init__(self, repository: str, install_url: str):
        self.repository = repository or "this repository"
        self.install_url = install_url or INSTALL_URL
        super().__init__(
            f"Zenik GitHub App is not installed on {self.repository}. "
            f"Install it: {self.install_url}")


def die(exc: BaseException) -> None:
    """Print a GitHub Actions error annotation and exit non-zero."""
    print(f"::error::{exc}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# HTTP plumbing (stdlib only — keep the client's runner dependency-free).
# ---------------------------------------------------------------------------
def _http_json(req: urllib.request.Request, timeout: int):
    """Perform the request; return the parsed JSON body ({} when empty).
    HTTPError/URLError propagate to the caller, which decides what they mean."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def _error_detail(e: urllib.error.HTTPError) -> tuple[str, Optional[dict]]:
    """(raw text, parsed JSON `detail` when present) of an error response."""
    raw = ""
    try:
        raw = e.read().decode()
    except Exception:
        pass
    parsed = None
    try:
        body = json.loads(raw) if raw else None
        if isinstance(body, dict):
            parsed = body.get("detail") if isinstance(body.get("detail"), dict) else body
    except (json.JSONDecodeError, ValueError):
        parsed = None
    return raw[:400], parsed


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------
def oidc_available() -> bool:
    return bool(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
                and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"))


def oidc_token(audience: str = OIDC_AUDIENCE) -> str:
    """A fresh GitHub Actions OIDC JWT for `audience`.

    GitHub's request URL already carries a query string, so the audience is
    appended with `&`. The response is `{"value": "<jwt>"}`.
    """
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not req_token:
        raise PlatformAuthError(OIDC_MISSING_MESSAGE)
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}audience={audience}", method="GET",
        headers={
            "Authorization": f"bearer {req_token}",
            "Accept": "application/json",
            "User-Agent": "zenik-action/1",
        },
    )
    try:
        body = _http_json(req, _OIDC_TIMEOUT)
    except urllib.error.HTTPError as e:
        detail, _ = _error_detail(e)
        raise PlatformAuthError(
            f"GitHub OIDC token request failed (HTTP {e.code}: {detail})")
    except urllib.error.URLError as e:
        raise PlatformAuthError(
            f"GitHub OIDC token request failed ({e.reason})")
    value = (body or {}).get("value") if isinstance(body, dict) else None
    if not value:
        raise PlatformAuthError("GitHub OIDC token response had no `value`")
    return value


# ---------------------------------------------------------------------------
# Platform calls
# ---------------------------------------------------------------------------
def platform_request(api_url: str, path: str, body: Optional[dict],
                     method: str = "POST",
                     timeout: int = _PLATFORM_TIMEOUT) -> Optional[dict]:
    """Call the platform with a fresh OIDC bearer.

    Returns the parsed response, or None on a transient failure (logged, never
    raised — the callers have local fallbacks). Raises PlatformAuthError /
    AppNotInstalledError when the platform says this run is not allowed to
    exist: those must fail the workflow, not degrade it.
    """
    if not api_url:
        print(f"[zenik] no zenik-api-url configured; skipping {path}")
        return None
    url = api_url.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {oidc_token()}",
            "User-Agent": "zenik-action/1",
        },
    )
    try:
        return _http_json(req, timeout)
    except urllib.error.HTTPError as e:
        detail, parsed = _error_detail(e)
        if e.code == 403 and (parsed or {}).get("error") == "app_not_installed":
            raise AppNotInstalledError(
                repository=(parsed or {}).get("repository") or "",
                install_url=(parsed or {}).get("install_url") or INSTALL_URL,
            )
        if e.code == 401:
            raise PlatformAuthError(
                f"Zenik platform rejected the OIDC token ({detail or 'no detail'})")
        if e.code == 403:
            raise PlatformAuthError(
                f"Zenik platform refused {path} ({detail or 'no detail'})")
        print(f"[zenik] {path} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        print(f"[zenik] {path} -> could not reach platform ({e.reason})")
    except Exception as e:  # noqa: BLE001
        print(f"[zenik] {path} -> unexpected error ({type(e).__name__})")
    return None


# ---------------------------------------------------------------------------
# The Zenik GitHub App's installation token
# ---------------------------------------------------------------------------
def _parse_expiry(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class InstallationToken:
    """The app's short-lived token for this repo, fetched once per run.

    `bearer()` hands back the current token, re-fetching when it is within
    the refresh margin of expiring (a long agent run can eat most of the hour).
    """

    def __init__(self, api_url: str):
        self.api_url = api_url
        self.token: str = ""
        self.expires_at: Optional[datetime] = None
        self.installation_id: Optional[int] = None
        self.app_slug: str = "zenik-ai"

    @classmethod
    def fetch(cls, api_url: str) -> "InstallationToken":
        inst = cls(api_url)
        inst.refresh()
        return inst

    def refresh(self) -> None:
        if not self.api_url:
            raise PlatformAuthError(
                "zenik-api-url is required: Zenik fetches its GitHub App token "
                "from the platform to post as zenik-ai[bot]")
        resp = platform_request(self.api_url, "/v1/github/token", None, "POST")
        if not resp or not resp.get("token"):
            raise PlatformAuthError(
                "could not obtain the Zenik GitHub App token from the platform "
                "(see the log above)")
        self.token = resp["token"]
        self.expires_at = _parse_expiry(resp.get("expires_at"))
        self.installation_id = resp.get("installation_id")
        self.app_slug = resp.get("app_slug") or "zenik-ai"
        left = self.seconds_left()
        print(f"[zenik] GitHub App token obtained (installation "
              f"{self.installation_id}, app {self.app_slug}"
              + (f", valid {int(left // 60)} min)" if left is not None else ")"))

    def seconds_left(self) -> Optional[float]:
        if self.expires_at is None:
            return None
        return (self.expires_at - datetime.now(timezone.utc)).total_seconds()

    def expiring_soon(self, margin: int = _REFRESH_MARGIN_SECONDS) -> bool:
        left = self.seconds_left()
        return left is not None and left < margin

    def bearer(self) -> str:
        if not self.token or self.expiring_soon():
            print("[zenik] GitHub App token is near expiry; re-fetching")
            self.refresh()
        return self.token

    @property
    def bot_login(self) -> str:
        return f"{self.app_slug}[bot]"
