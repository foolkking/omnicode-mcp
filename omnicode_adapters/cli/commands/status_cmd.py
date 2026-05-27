"""omnicode status — show index and service status."""

import sys


def run():
    """Display current system status by querying the running server."""
    import httpx

    base = "http://127.0.0.1:6789"

    try:
        with httpx.Client(base_url=base, timeout=10.0) as client:
            # Health
            r = client.get("/health")
            if r.status_code != 200:
                print(f"❌ Server unhealthy: HTTP {r.status_code}")
                sys.exit(1)

            health = r.json().get("result", {})
            components = health.get("components", {})
            wd = health.get("working_directory", "?")

            print("✅ OmniCode-MCP is running")
            print(f"   Working directory: {wd}")
            print()

            # Services
            print("   Services:")
            for svc, ok in components.items():
                icon = "✅" if ok else "❌"
                print(f"     {icon} {svc}")
            print()

            # Search stats
            r = client.get("/search/stats")
            if r.status_code == 200:
                stats = r.json().get("result", {}).get("index_stats", {})
                print("   Index:")
                print(f"     Files:   {stats.get('total_files', 0)}")
                print(f"     Chunks:  {stats.get('total_chunks', 0)}")
                print(f"     Symbols: {stats.get('total_symbols', 0)}")
                status = "indexed" if stats.get("total_files", 0) > 0 else "empty (run: omnicode index)"
                print(f"     Status:  {status}")

    except httpx.ConnectError:
        print("❌ Server not running at http://127.0.0.1:6789")
        print("   Start it with: omnicode serve")
        sys.exit(1)
