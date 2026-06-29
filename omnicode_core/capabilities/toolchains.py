"""Toolchain discovery for production-grade language capabilities.

The detector is intentionally side-effect free: it only checks PATH and build
markers. Starting language servers belongs to the diagnostics/graph layers.
"""

from __future__ import annotations

import shutil
import os
import time
from pathlib import Path
from typing import Any

_TOOL_STATUS_CACHE: dict[
    tuple[str, str, object],
    tuple[float, dict[str, Any]],
] = {}
_TOOL_STATUS_TTL_SECONDS = 5.0

_TOOL_HINTS = {
    "jdtls": "Install Eclipse JDT LS and put the jdtls wrapper on PATH.",
    "metals": "Install Metals with coursier: cs install metals.",
    "mvn": "Install Apache Maven for Java workspace import.",
    "gradle": "Install Gradle or use a project Gradle wrapper.",
    "sbt": "Install sbt for Scala workspace import.",
    "bloop": "Install Bloop or let Metals generate a Bloop workspace.",
    "javac": "Install a JDK and put javac on PATH.",
    "scalac": "Install Scala or use sbt/Metals diagnostics.",
}


def _tool_status(name: str) -> dict[str, Any]:
    cache_key = (name, os.environ.get("PATH", ""), shutil.which)
    cached = _TOOL_STATUS_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] <= _TOOL_STATUS_TTL_SECONDS:
        return dict(cached[1])
    path = shutil.which(name)
    payload = {
        "name": name,
        "available": bool(path),
        "path": path,
        "install_hint": _TOOL_HINTS.get(name, f"Install {name} and put it on PATH."),
    }
    _TOOL_STATUS_CACHE[cache_key] = (now, payload)
    return dict(payload)


def _known_runtime_tool_status(
    name: str,
    runtime_row: dict[str, Any],
) -> dict[str, Any]:
    if "available" not in runtime_row:
        return _tool_status(name)
    return {
        "name": name,
        "available": bool(runtime_row.get("available")),
        "path": runtime_row.get("path"),
        "install_hint": _TOOL_HINTS.get(
            name,
            f"Install {name} and put it on PATH.",
        ),
    }


def _not_required_tool_status(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "available": False,
        "path": None,
        "checked": False,
        "reason": "not_required_by_workspace_build_markers",
        "install_hint": _TOOL_HINTS.get(
            name,
            f"Install {name} and put it on PATH.",
        ),
    }


def _has_any(root: Path, names: list[str]) -> bool:
    return any((root / name).exists() for name in names)


def detect_workspace_toolchains(
    workspace_root: str | Path | None = None,
    *,
    lsp_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable, serializable language toolchain status payload."""

    root = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
    lsp_runtime = lsp_runtime if isinstance(lsp_runtime, dict) else {}
    java_runtime = (
        lsp_runtime.get("java")
        if isinstance(lsp_runtime.get("java"), dict)
        else {}
    )
    scala_runtime = (
        lsp_runtime.get("scala")
        if isinstance(lsp_runtime.get("scala"), dict)
        else {}
    )
    build_files = {
        "maven": (root / "pom.xml").exists(),
        "gradle": _has_any(root, ["build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"]),
        "gradle_wrapper": _has_any(root, ["gradlew", "gradlew.bat"]),
        "sbt": _has_any(root, ["build.sbt", "project/build.properties"]),
        "bloop": (root / ".bloop").exists(),
    }
    tools = {
        "jdtls": _known_runtime_tool_status("jdtls", java_runtime),
        "metals": _known_runtime_tool_status("metals", scala_runtime),
        "mvn": (
            _tool_status("mvn")
            if build_files["maven"]
            else _not_required_tool_status("mvn")
        ),
        "gradle": (
            _tool_status("gradle")
            if build_files["gradle"] and not build_files["gradle_wrapper"]
            else _not_required_tool_status("gradle")
        ),
        "sbt": (
            _tool_status("sbt")
            if build_files["sbt"]
            else _not_required_tool_status("sbt")
        ),
        "bloop": (
            _tool_status("bloop")
            if build_files["bloop"]
            else _not_required_tool_status("bloop")
        ),
        "javac": _tool_status("javac"),
        "scalac": _tool_status("scalac"),
    }
    java_build_ready = (
        (build_files["maven"] and tools["mvn"]["available"])
        or (build_files["gradle"] and (tools["gradle"]["available"] or build_files["gradle_wrapper"]))
        or not (build_files["maven"] or build_files["gradle"])
    )
    scala_build_ready = (
        (build_files["sbt"] and tools["sbt"]["available"])
        or (build_files["bloop"] and tools["bloop"]["available"])
        or (
            build_files["gradle"]
            and (tools["gradle"]["available"] or build_files["gradle_wrapper"])
        )
        or not (
            build_files["sbt"]
            or build_files["bloop"]
            or build_files["gradle"]
        )
    )

    java_lsp_installed = bool(
        tools["jdtls"]["available"] or java_runtime.get("available")
    )
    scala_lsp_installed = bool(
        tools["metals"]["available"] or scala_runtime.get("available")
    )
    java_toolchain_ready = bool(java_lsp_installed and java_build_ready)
    scala_toolchain_ready = bool(scala_lsp_installed and scala_build_ready)
    java_runtime_ready = bool(
        java_runtime.get("running") and java_runtime.get("initialized", True)
    )
    scala_runtime_ready = bool(
        scala_runtime.get("running") and scala_runtime.get("initialized", True)
    )
    java_ready = bool(java_toolchain_ready and java_runtime_ready)
    scala_ready = bool(scala_toolchain_ready and scala_runtime_ready)

    return {
        "workspace_root": str(root),
        "tools": tools,
        "build_files": build_files,
        "java": {
            "workspace_diagnostics_ready": java_ready,
            "toolchain_ready": java_toolchain_ready,
            "runtime_ready": java_runtime_ready,
            "lsp": "jdtls",
            "build_ready": bool(java_build_ready),
            "build_system": (
                "maven"
                if build_files["maven"]
                else "gradle"
                if build_files["gradle"]
                else "standalone"
            ),
            "reason": (
                "ready"
                if java_ready
                else (
                    "jdtls_unavailable"
                    if not java_lsp_installed
                    else (
                        "java_build_tool_unavailable"
                        if not java_build_ready
                        else "jdtls_not_started"
                    )
                )
            ),
        },
        "scala": {
            "workspace_diagnostics_ready": scala_ready,
            "toolchain_ready": scala_toolchain_ready,
            "runtime_ready": scala_runtime_ready,
            "lsp": "metals",
            "build_ready": bool(scala_build_ready),
            "build_system": (
                "sbt"
                if build_files["sbt"]
                else "bloop"
                if build_files["bloop"]
                else "gradle"
                if build_files["gradle"]
                else "standalone"
            ),
            "reason": (
                "ready"
                if scala_ready
                else (
                    "metals_unavailable"
                    if not scala_lsp_installed
                    else (
                        "scala_build_tool_unavailable"
                        if not scala_build_ready
                        else "metals_not_started"
                    )
                )
            ),
        },
    }


__all__ = ["detect_workspace_toolchains"]
