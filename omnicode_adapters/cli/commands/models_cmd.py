"""omnicode models - manage embedding model cache."""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stderr
from io import StringIO
from typing import Optional

from omnicode_core.embeddings.models import (
    SUPPORTED_EMBEDDING_MODELS,
    embedding_status,
    pull_model,
)


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def run(
    action: str,
    *,
    model: Optional[str] = None,
    cache_dir: Optional[str] = None,
    revision: Optional[str] = None,
    device: Optional[str] = None,
    json_output: bool = False,
) -> None:
    action = (action or "status").strip().lower()
    if action == "list":
        data = {
            "supported_models": [
                {"model": name, **meta}
                for name, meta in sorted(SUPPORTED_EMBEDDING_MODELS.items())
            ],
            "default_local": "sentence-transformers/all-MiniLM-L6-v2",
            "default_cloud": "BAAI/bge-small-en-v1.5",
        }
        if json_output:
            _print_json(data)
            return
        print("Supported embedding models:")
        for item in data["supported_models"]:
            print(
                f"  - {item['model']} "
                f"(dim={item.get('dimension')}, "
                f"recommended={','.join(item.get('recommended_for') or [])})"
            )
        print(f"Default local: {data['default_local']}")
        print(f"Default cloud: {data['default_cloud']}")
        return

    if action == "status":
        data = embedding_status(
            model,
            cache_dir=cache_dir,
            revision=revision,
            device=device,
        )
        if json_output:
            _print_json(data)
            return
        print("Embedding model status:")
        print(f"   model:            {data.get('model')}")
        print(f"   dimension:        {data.get('dimension')}")
        print(f"   cache_dir:        {data.get('cache_dir')}")
        print(f"   local_files_only: {data.get('local_files_only')}")
        print(f"   cached:           {data.get('cached')}")
        print(f"   available:        {data.get('available')}")
        if data.get("error_code"):
            print(f"   error_code:       {data.get('error_code')}")
            print(f"   error:            {data.get('error')}")
        return

    if action == "pull":
        target = model
        if not target:
            if json_output:
                _print_json({
                    "ok": False,
                    "error_code": "MISSING_MODEL",
                    "error": "models pull requires --model",
                })
                sys.exit(2)
            print("models pull requires --model")
            sys.exit(2)
        try:
            if json_output:
                captured_stderr = StringIO()
                with redirect_stderr(captured_stderr):
                    data = pull_model(
                        target,
                        cache_dir=cache_dir,
                        revision=revision,
                        device=device,
                    )
                data = dict(data)
                data.setdefault("ok", True)
                stderr_text = captured_stderr.getvalue().strip()
                if stderr_text:
                    data["diagnostics"] = stderr_text.splitlines()[-20:]
            else:
                data = pull_model(
                    target,
                    cache_dir=cache_dir,
                    revision=revision,
                    device=device,
                )
        except Exception as exc:
            if json_output:
                _print_json({
                    "ok": False,
                    "model": target,
                    "cache_dir": cache_dir,
                    "error_code": exc.__class__.__name__,
                    "error": str(exc),
                })
                sys.exit(1)
            print(f"Model pull failed: {exc}")
            sys.exit(1)
        if json_output:
            _print_json(data)
            return
        print("Model cached.")
        print(f"   model:     {data.get('model')}")
        print(f"   dimension: {data.get('dimension')}")
        print(f"   cache_dir: {data.get('cache_dir')}")
        return

    print("Unknown models action. Use: list, pull, status")
    sys.exit(2)
