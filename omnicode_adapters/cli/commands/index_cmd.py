"""omnicode index - run explicit backend indexing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional


def _backend_url(backend_url: Optional[str], port: Optional[int]) -> str:
    if backend_url:
        return backend_url.rstrip("/")
    if port:
        return f"http://127.0.0.1:{port}"
    return "http://127.0.0.1:6789"


def _register_workspace(
    client: Any,
    *,
    workspace: Optional[str],
    workspace_id: Optional[str],
) -> None:
    if not workspace or not workspace_id:
        return
    root = str(Path(workspace).expanduser().resolve())
    response = client.post(
        "/workspaces",
        json={
            "workspace_id": workspace_id,
            "name": workspace_id,
            "path": root,
            "set_active": True,
        },
    )
    if response.status_code not in {200, 201}:
        print(f"Workspace registration failed: HTTP {response.status_code}")
        print(response.text[:500])
        sys.exit(1)


def run(
    force: bool = False,
    background: bool = False,
    backend_url: Optional[str] = None,
    port: Optional[int] = None,
    workspace: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> None:
    """Trigger codebase indexing on a selected FastAPI backend."""
    import httpx

    base = _backend_url(backend_url, port)
    headers = {"X-Omnicode-Workspace": workspace_id} if workspace_id else {}
    params: dict[str, Any] = {"force": bool(force), "background": bool(background)}
    if workspace_id:
        params["workspace_id"] = workspace_id

    print(("Force rebuilding" if force else "Incremental indexing") + " codebase...")
    print(f"   backend: {base}")
    if workspace_id:
        print(f"   workspace_id: {workspace_id}")
    if workspace:
        print(f"   workspace: {Path(workspace).expanduser().resolve()}")
    print()

    try:
        with httpx.Client(base_url=base, timeout=300.0) as client:
            _register_workspace(
                client,
                workspace=workspace,
                workspace_id=workspace_id,
            )
            response = client.post("/search/index", params=params, headers=headers)
            if response.status_code != 200:
                print(f"Indexing failed: HTTP {response.status_code}")
                print(response.text[:500])
                sys.exit(1)
            body = response.json()
            if body.get("success") is False or body.get("ok") is False:
                print("Indexing failed:")
                print(str(body)[:800])
                sys.exit(1)
            data = body.get("result", body) if isinstance(body, dict) else {}
            if data.get("background"):
                job = data.get("job") or {}
                print("Indexing started in background.")
                print(f"   Job:     {job.get('job_id', '?')}")
                print(f"   State:   {job.get('state', '?')}")
                if workspace_id:
                    print(
                        "   Status:  "
                        f"{base}/search/index/status?workspace_id={workspace_id}"
                    )
                return

            stats = data.get("stats") or {}
            print("Indexing complete.")
            if stats:
                print(f"   Files:   {stats.get('total_files', stats.get('files', '?'))}")
                print(f"   Chunks:  {stats.get('total_chunks', stats.get('chunks', '?'))}")
                print(f"   Symbols: {stats.get('total_symbols', stats.get('symbols', '?'))}")
            if data.get("snapshot_store_used"):
                print("   Source:  snapshot_store")
    except httpx.ConnectError:
        print(f"Cannot connect to the server at {base}")
        print("Start the server first: omnicode serve")
        sys.exit(1)
