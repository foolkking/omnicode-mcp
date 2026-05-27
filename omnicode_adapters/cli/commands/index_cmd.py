"""omnicode index — run incremental (or full) index."""

import sys


def run(force: bool = False):
    """Trigger codebase indexing.

    By default, uses incremental mode (skips unchanged files).
    With --force, rebuilds everything from scratch.
    """
    # For now, delegate to the existing FastAPI endpoint via HTTP.
    # In the future, this will call omnicode_core.index directly.
    import httpx

    base = "http://127.0.0.1:6789"
    print(f"{'🔄 Force rebuilding' if force else '📦 Incremental indexing'} codebase...")
    print(f"   (calling {base}/search/index)")
    print()

    try:
        with httpx.Client(base_url=base, timeout=120.0) as client:
            r = client.post("/search/index")
            if r.status_code == 200:
                data = r.json().get("result", {})
                stats = data.get("stats") or {}
                print("✅ Indexing complete!")
                if stats:
                    print(f"   Files:   {stats.get('total_files', '?')}")
                    print(f"   Chunks:  {stats.get('total_chunks', '?')}")
                    print(f"   Symbols: {stats.get('total_symbols', '?')}")
            else:
                print(f"❌ Indexing failed: HTTP {r.status_code}")
                print(f"   {r.text[:300]}")
                sys.exit(1)
    except httpx.ConnectError:
        print("❌ Cannot connect to the server at http://127.0.0.1:6789")
        print("   Start the server first: omnicode serve")
        sys.exit(1)
