"""HTTP client for the hybrid /sync protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from omnicode_core.workspace.sync_queue import SyncBatch


@dataclass(frozen=True)
class SyncClientResult:
    ok: bool
    error: Optional[str] = None
    status_code: Optional[int] = None
    retryable: bool = False
    cloud_unavailable: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncResult(SyncClientResult):
    accepted_revision: Optional[int] = None
    indexed_revision: Optional[int] = None


@dataclass(frozen=True)
class BarrierResult(SyncClientResult):
    ready: bool = False
    stale: bool = False
    accepted_revision: Optional[int] = None
    indexed_revision: Optional[int] = None


class SyncClient:
    """Synchronous client for cloud sync endpoints.

    The client is intentionally sync because the existing local watcher is
    sync. Async wrappers can call it from a thread executor later.
    """

    def __init__(
        self,
        *,
        remote: str,
        workspace_id: str,
        token: str = "",
        executor: str = "hybrid",
        client_id: str = "",
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        if not remote:
            raise ValueError("remote is required")
        if not workspace_id:
            raise ValueError("workspace_id is required")
        self.remote = remote.rstrip("/")
        self.workspace_id = workspace_id.strip()
        self.token = token or ""
        self.executor = executor or "hybrid"
        self.client_id = client_id or ""
        self._client = client or httpx.Client(
            base_url=self.remote, timeout=timeout
        )

    def _headers(self, client_id: Optional[str] = None) -> dict[str, str]:
        headers = {
            "X-Omnicode-Workspace": self.workspace_id,
            "X-Omnicode-Executor": self.executor,
        }
        cid = client_id or self.client_id
        if cid:
            headers["X-Omnicode-Client"] = cid
        if self.token:
            headers["X-API-Key"] = self.token
        return headers

    def health(self) -> SyncClientResult:
        try:
            response = self._client.get("/health", headers=self._headers())
        except httpx.HTTPError as exc:
            return SyncClientResult(
                ok=False, error=str(exc), retryable=True, cloud_unavailable=True,
            )
        return SyncClientResult(
            ok=response.status_code == 200,
            status_code=response.status_code,
            error=None if response.status_code == 200 else response.text[:200],
            retryable=response.status_code >= 500,
            cloud_unavailable=response.status_code >= 500,
            payload=_json_payload(response),
        )

    def capabilities(self) -> SyncClientResult:
        return self._request_json("GET", "/capabilities")

    def push_batch(self, batch: SyncBatch) -> SyncResult:
        result = self._request_json(
            "POST",
            "/sync/batch",
            json=batch.to_payload(),
            client_id=batch.client_id,
        )
        return _sync_result_from(result)

    def delete_batch(self, paths: list[str]) -> SyncResult:
        payload = {
            "client_id": self.client_id,
            "files": [],
            "deletes": [{"path": p} for p in paths],
        }
        result = self._request_json("POST", "/sync/batch", json=payload)
        return _sync_result_from(result)

    def status(self) -> SyncClientResult:
        return self._request_json("GET", "/sync/status")

    def barrier(
        self,
        *,
        min_revision: int,
        paths: Optional[list[str]] = None,
        wait_ms: int = 0,
    ) -> BarrierResult:
        payload = {
            "min_revision": min_revision,
            "paths": paths or [],
            "wait_ms": wait_ms,
        }
        result = self._request_json("POST", "/sync/barrier", json=payload)
        body = result.payload.get("result", result.payload)
        if not isinstance(body, dict):
            body = {}
        return BarrierResult(
            ok=result.ok,
            error=result.error,
            status_code=result.status_code,
            retryable=result.retryable,
            cloud_unavailable=result.cloud_unavailable,
            payload=result.payload,
            ready=bool(body.get("ready", False)),
            stale=bool(body.get("stale", False)),
            accepted_revision=_optional_int(body.get("accepted_revision")),
            indexed_revision=_optional_int(body.get("indexed_revision")),
        )

    def close(self) -> None:
        self._client.close()

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        client_id: Optional[str] = None,
    ) -> SyncClientResult:
        try:
            response = self._client.request(
                method, path, json=json, headers=self._headers(client_id),
            )
        except httpx.HTTPError as exc:
            return SyncClientResult(
                ok=False,
                error=str(exc),
                retryable=True,
                cloud_unavailable=True,
            )

        payload = _json_payload(response)
        ok = response.status_code == 200 and _payload_ok(payload)
        error = None if ok else _error_text(response, payload)
        return SyncClientResult(
            ok=ok,
            error=error,
            status_code=response.status_code,
            retryable=response.status_code >= 500,
            cloud_unavailable=response.status_code in (401, 403)
            or response.status_code >= 500,
            payload=payload,
        )


def _json_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {"raw": response.text}
    return data if isinstance(data, dict) else {"result": data}


def _payload_ok(payload: dict[str, Any]) -> bool:
    if "ok" in payload:
        return bool(payload.get("ok"))
    if "success" in payload:
        return bool(payload.get("success"))
    return "error" not in payload


def _error_text(response: httpx.Response, payload: dict[str, Any]) -> str:
    for key in ("error", "message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("error", "message", "detail"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return f"HTTP {response.status_code}: {response.text[:200]}"


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sync_result_from(result: SyncClientResult) -> SyncResult:
    body = result.payload.get("result", result.payload)
    if not isinstance(body, dict):
        body = {}
    return SyncResult(
        ok=result.ok,
        error=result.error,
        status_code=result.status_code,
        retryable=result.retryable,
        cloud_unavailable=result.cloud_unavailable,
        payload=result.payload,
        accepted_revision=_optional_int(body.get("accepted_revision")),
        indexed_revision=_optional_int(body.get("indexed_revision")),
    )


__all__ = [
    "BarrierResult",
    "SyncClient",
    "SyncClientResult",
    "SyncResult",
]
