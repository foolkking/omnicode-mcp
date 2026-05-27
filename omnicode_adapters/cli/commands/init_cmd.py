"""omnicode init — initialize .data/ directory for the current project."""

from pathlib import Path


def run():
    """Create the .data/ directory structure for a new project."""
    cwd = Path.cwd()
    data_dir = cwd / ".data"

    if data_dir.exists():
        print(f"✅ .data/ already exists at {data_dir}")
        print(f"   vector_store.db: {'exists' if (data_dir / 'vector_store.db').exists() else 'not yet (run omnicode index)'}")
        print(f"   metadata.db:     {'exists' if (data_dir / 'metadata.db').exists() else 'not yet'}")
        return

    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"✅ Created {data_dir}/")
    print()
    print("Next steps:")
    print("  1. Start the server:  omnicode serve")
    print("  2. Build the index:   omnicode index")
    print("     (or click 'Rebuild Index' in the Web Console)")
