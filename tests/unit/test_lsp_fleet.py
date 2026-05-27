"""Smoke tests for the LSP fleet table (Wave 2 W2-7).

We don't try to actually start the language servers in CI — most of
them aren't installed on the runners and we don't want CI minutes
swallowed by JVM warmup. These tests just sanity-check that:

* Every entry has the three required keys.
* Extension lists don't overlap (so language detection is unambiguous).
* The `_detect_language` helper picks the right key for each ext.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnicode_core.lsp.bridge import LSP_SERVERS, LSPBridge


def test_every_server_has_required_keys():
    required = {"command", "install_hint", "extensions"}
    for lang, spec in LSP_SERVERS.items():
        missing = required - spec.keys()
        assert not missing, f"{lang}: missing keys {missing}"


def test_command_is_non_empty_list():
    for lang, spec in LSP_SERVERS.items():
        assert isinstance(spec["command"], list), lang
        assert spec["command"], f"{lang}: empty command list"
        assert all(isinstance(c, str) and c for c in spec["command"]), lang


def test_extensions_have_leading_dot():
    for lang, spec in LSP_SERVERS.items():
        for ext in spec["extensions"]:
            assert ext.startswith("."), f"{lang}: ext {ext!r} missing leading dot"


def test_extensions_dont_clash():
    """No extension should map to two different language keys —
    `_detect_language` returns the *first* hit and order isn't stable."""
    seen: dict[str, str] = {}
    for lang, spec in LSP_SERVERS.items():
        for ext in spec["extensions"]:
            assert ext not in seen, (
                f"{ext} double-mapped to {seen[ext]} and {lang}"
            )
            seen[ext] = lang


def test_w2_7_languages_present():
    """All five W2-7 additions must be in the table."""
    for lang in ("ruby", "php", "java", "kotlin", "csharp"):
        assert lang in LSP_SERVERS, f"missing {lang}"


@pytest.mark.parametrize(
    "ext,expected",
    [
        (".py", "python"),
        (".ts", "typescript"),
        (".tsx", "typescript"),
        (".rs", "rust"),
        (".go", "go"),
        (".cpp", "cpp"),
        (".rb", "ruby"),
        (".php", "php"),
        (".java", "java"),
        (".kt", "kotlin"),
        (".kts", "kotlin"),
        (".cs", "csharp"),
        (".unknown", None),
    ],
)
def test_detect_language(tmp_path: Path, ext, expected):
    bridge = LSPBridge(working_dir=str(tmp_path))
    assert bridge._detect_language(f"file{ext}") == expected


def test_install_hint_mentions_a_well_known_installer():
    """Light readability check — every hint should reference at least
    one of the common package managers so users know where to look."""
    keywords = (
        "pip", "npm", "go install", "cargo", "rustup", "brew", "apt",
        "gem", "dotnet", "https://", "github", "Eclipse",
    )
    for lang, spec in LSP_SERVERS.items():
        hint = spec["install_hint"]
        assert any(k in hint for k in keywords), (
            f"{lang}: hint {hint!r} doesn't mention a known installer"
        )


# ---------------------------------------------------------------------------
# Optional integration-style probes — only run when the server binary is
# actually on PATH. They simply check that `shutil.which()` agrees with
# the table, not that the server can serve requests.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lang", sorted(LSP_SERVERS.keys()))
def test_binary_resolution(lang):
    binary = LSP_SERVERS[lang]["command"][0]
    found = shutil.which(binary)
    if not found:
        pytest.skip(f"{binary} not installed on this runner")
    assert Path(found).exists()
