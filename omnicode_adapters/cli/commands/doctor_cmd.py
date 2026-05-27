"""omnicode doctor — check environment health."""

import shutil
import sys
from pathlib import Path


def run():
    """Check that all required tools and dependencies are available."""
    print("🩺 OmniCode-MCP Environment Check")
    print("=" * 50)
    issues = []

    # Python version
    v = sys.version_info
    print(f"\n  Python: {v.major}.{v.minor}.{v.micro}", end="")
    if v.minor >= 11:
        print(" ✅")
    else:
        print(" ⚠️  (3.11+ recommended)")
        issues.append("Python 3.11+ recommended")

    # Key packages
    packages = [
        ("fastapi", "FastAPI"),
        ("uvicorn", "Uvicorn"),
        ("faiss", "FAISS"),
        ("sentence_transformers", "sentence-transformers"),
        ("tree_sitter", "tree-sitter"),
        ("httpx", "httpx"),
        ("litellm", "LiteLLM (optional)"),
        ("mcp", "MCP SDK"),
    ]
    print("\n  Packages:")
    for mod, name in packages:
        try:
            __import__(mod)
            print(f"    ✅ {name}")
        except ImportError:
            optional = "optional" in name.lower()
            icon = "⚠️ " if optional else "❌"
            print(f"    {icon} {name} — not installed")
            if not optional:
                issues.append(f"{name} not installed")

    # LSP servers
    print("\n  Language Servers:")
    lsp_servers = [
        ("pyright", "Pyright (Python)"),
        ("typescript-language-server", "tsserver (TypeScript)"),
        ("gopls", "gopls (Go)"),
        ("rust-analyzer", "rust-analyzer (Rust)"),
        ("clangd", "clangd (C/C++)"),
    ]
    for cmd, name in lsp_servers:
        found = shutil.which(cmd)
        if found:
            print(f"    ✅ {name}")
        else:
            print(f"    ⚠️  {name} — not found (optional)")

    # Static analysis tools
    print("\n  Static Analysis:")
    tools = [
        ("ruff", "ruff (Python)"),
        ("mypy", "mypy (Python types)"),
        ("eslint", "eslint (JS/TS)"),
        ("cppcheck", "cppcheck (C/C++)"),
    ]
    for cmd, name in tools:
        found = shutil.which(cmd)
        if found:
            print(f"    ✅ {name}")
        else:
            print(f"    ⚠️  {name} — not found (optional)")

    # Data directory
    print("\n  Data:")
    data_dir = Path.cwd() / ".data"
    if data_dir.exists():
        files = list(data_dir.glob("*"))
        print(f"    ✅ .data/ exists ({len(files)} files)")
    else:
        print("    ⚠️  .data/ not found — run: omnicode init")

    # Port check
    print("\n  Network:")
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", 6789))
        s.close()
        if result == 0:
            print("    ✅ Port 6789 — server is running")
        else:
            print("    ⚠️  Port 6789 — server not running")
    except Exception:
        print("    ⚠️  Port 6789 — could not check")

    # Summary
    print("\n" + "=" * 50)
    if issues:
        print(f"  ⚠️  {len(issues)} issue(s) found:")
        for i in issues:
            print(f"    - {i}")
    else:
        print("  ✅ All checks passed!")
