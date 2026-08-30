"""HTTP client for zenik-platform. urllib only — see embed.py for why."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class PlatformError(RuntimeError):
    pass


class PlatformClient:
    def __init__(self, base_url: str | None = None, client_key: str | None = None):
        self.base_url = (base_url or os.getenv("ZENIK_PLATFORM_URL", "http://127.0.0.1:8000")).rstrip("/")
        self.client_key = client_key or os.getenv("ZENIK_CLIENT_KEY", "")
        if not self.client_key:
            raise PlatformError("ZENIK_CLIENT_KEY is not set")

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.client_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise PlatformError(
                f"{method} {path} -> {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise PlatformError(f"{method} {path} unreachable: {exc.reason}") from exc

    def health(self) -> dict:
        return self._call("GET", "/health")

    def register_repo(self, full_name: str, default_branch: str = "main") -> dict:
        return self._call("POST", "/v1/repos",
                          {"full_name": full_name, "default_branch": default_branch})

    def send_batch(self, repo_id: str, batch: dict) -> dict:
        return self._call("POST", f"/v1/index/{repo_id}/batch", batch)

    def commit_index(self, repo_id: str, commit_sha: str, replace: bool = True) -> dict:
        return self._call("POST", f"/v1/index/{repo_id}/commit",
                          {"commit_sha": commit_sha, "replace": replace})

    def impact(self, payload: dict) -> dict:
        return self._call("POST", "/v1/impact", payload)

    def telemetry(self, payload: dict) -> dict:
        return self._call("POST", "/v1/telemetry", payload)
