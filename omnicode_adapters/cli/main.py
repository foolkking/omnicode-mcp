"""
OmniCode CLI — unified entry point.

Usage:
    omnicode init              Initialize .data/ in the current directory
    omnicode index             Run incremental index
    omnicode status            Show index and service status
    omnicode mcp               Start MCP server / local-to-cloud bridge
    omnicode serve             Start HTTP API + Web Console (default)
    omnicode serve --headless  Start HTTP API only (no Web UI)
    omnicode serve --console   Start HTTP API + Web Console (explicit)
    omnicode dev               Start in development mode (console + reload)
    omnicode agent             Local-side file-sync agent (hybrid mode)
    omnicode rotate-master-key Rotate provider-DB encryption master key
    omnicode doctor            Check environment (Python, LSP, models, ports)
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="omnicode",
        description="OmniCode-MCP — Codebase Intelligence Layer",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- init ---
    subparsers.add_parser("init", help="Initialize .data/ directory")

    # --- index ---
    idx_parser = subparsers.add_parser("index", help="Run incremental index")
    idx_parser.add_argument("--force", action="store_true", help="Force full rebuild")
    idx_parser.add_argument(
        "--background",
        action="store_true",
        help="Start snapshot indexing in the background when supported.",
    )
    idx_parser.add_argument(
        "--scope",
        choices=("semantic", "exact_policy"),
        default="semantic",
        help=(
            "Snapshot indexing scope. 'semantic' forces full semantic bootstrap; "
            "'exact_policy' indexes only files allowed by the semantic policy."
        ),
    )
    idx_parser.add_argument(
        "--status",
        action="store_true",
        help="Show background snapshot indexing job status instead of starting one.",
    )
    idx_parser.add_argument(
        "--backend-url",
        default=None,
        help="FastAPI backend URL to index (default: http://127.0.0.1:6789).",
    )
    idx_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Shortcut for --backend-url http://127.0.0.1:<port>.",
    )
    idx_parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace root to register before indexing.",
    )
    idx_parser.add_argument(
        "--workspace-id",
        default=None,
        help="Logical workspace id to index.",
    )

    # --- status ---
    subparsers.add_parser("status", help="Show index and service status")

    # --- mcp ---
    mcp_parser = subparsers.add_parser("mcp", help="Start MCP server")
    mcp_parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=None,
        help="MCP transport (default: stdio).",
    )
    mcp_parser.add_argument(
        "--host",
        default=None,
        help="Bind host for sse/streamable-http transports.",
    )
    mcp_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port for sse/streamable-http transports (default: 6790).",
    )
    mcp_parser.add_argument(
        "--mount-path",
        default=None,
        help="URL prefix for sse/streamable-http transports.",
    )
    mcp_parser.add_argument(
        "--auth",
        choices=("auto", "required", "off"),
        default="auto",
        help="Transport auth posture for sse/streamable-http.",
    )
    mcp_parser.add_argument(
        "--backend-url",
        default=None,
        help="FastAPI backend URL for local or remote tool execution.",
    )
    mcp_parser.add_argument(
        "--backend-token",
        default=None,
        help="FastAPI backend API token.",
    )
    mcp_parser.add_argument(
        "--workspace",
        default=None,
        help="Local workspace root for stdio/hybrid MCP sessions.",
    )
    mcp_parser.add_argument(
        "--workspace-id",
        default=None,
        help="Logical workspace id sent to the backend.",
    )
    mcp_parser.add_argument(
        "--executor",
        choices=("local", "remote", "hybrid", "auto"),
        default=None,
        help="Execution locality policy for workspace-aware MCP calls.",
    )
    mcp_parser.add_argument(
        "--sync-mode",
        choices=("off", "watch", "smart", "strict"),
        default=None,
        help="Local sync policy for hybrid MCP sessions.",
    )
    mcp_parser.add_argument(
        "--agent",
        choices=("auto", "external", "off"),
        default=None,
        help="Embedded local watcher policy for hybrid MCP sessions.",
    )
    mcp_parser.add_argument(
        "--llm-mode",
        choices=("off", "local", "remote", "auto"),
        default=None,
        help="LLM capability policy exposed to MCP tools.",
    )
    mcp_parser.add_argument(
        "--embedding-mode",
        choices=("off", "local", "cloud"),
        default=None,
        help="Embedding capability policy for local/hybrid search.",
    )

    # --- serve ---
    serve_parser = subparsers.add_parser("serve", help="Start HTTP API server")
    serve_parser.add_argument("--headless", action="store_true", help="No Web UI")
    serve_parser.add_argument("--console", action="store_true", help="With Web Console (default)")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    serve_parser.add_argument("--port", type=int, default=6789, help="Bind port")
    serve_parser.add_argument("--reload", action="store_true", help="Auto-reload on file changes")
    serve_parser.add_argument(
        "--state-dir",
        default=None,
        help="Root directory for server state such as cloud sync snapshots.",
    )
    serve_parser.add_argument(
        "--workspace-store",
        default=None,
        help="Explicit cloud workspace snapshot store root.",
    )
    serve_parser.add_argument(
        "--content-store",
        choices=("object",),
        default=None,
        help="Cloud sync content store backend. Currently only 'object'.",
    )
    serve_parser.add_argument(
        "--materialize-mirror",
        choices=("true", "false"),
        default=None,
        help="Write a readonly mirror tree beside the object store.",
    )
    serve_parser.add_argument(
        "--mirror-readonly",
        choices=("true", "false"),
        default=None,
        help="Mark materialized mirror files readonly after sync writes.",
    )
    serve_parser.add_argument(
        "--mode",
        choices=("local", "cloud", "hybrid", "cloud-index", "local-readonly"),
        default="local",
        help=(
            "Deployment mode (Wave 1, gap §13). "
            "local: single-user dev (defaults). "
            "local-readonly: like local but blocks all writes — useful "
            "for demoing the API to a colleague over localhost. "
            "cloud: shared/remote — turns on read-only + blocks /patch/apply by default. "
            "hybrid: cloud index + local apply — read-only off, apply still gated."
        ),
    )

    # --- dev ---
    dev_parser = subparsers.add_parser("dev", help="Development mode (console + reload)")
    dev_parser.add_argument("--host", default="127.0.0.1")
    dev_parser.add_argument("--port", type=int, default=6789)

    # --- agent (Wave 2, W2-2) ---
    agent_parser = subparsers.add_parser(
        "agent", help="Local file-sync agent — pushes changes to a remote OmniCode"
    )
    agent_parser.add_argument(
        "--remote",
        default=None,
        help="Remote OmniCode URL (e.g. https://omnicode.example.com). "
        "Defaults to OMNICODE_REMOTE env var.",
    )
    agent_parser.add_argument(
        "--token",
        default=None,
        help="API key / RBAC token for the remote. Defaults to OMNICODE_API_KEY "
        "or OMNICODE_AGENT_TOKEN env vars.",
    )
    agent_parser.add_argument(
        "--workspace",
        default=".",
        help="Local working tree to watch (default: cwd).",
    )
    agent_parser.add_argument(
        "--workspace-id",
        default=None,
        help="Logical workspace id to send as X-Omnicode-Workspace.",
    )
    agent_parser.add_argument(
        "--no-initial-sync",
        action="store_true",
        help="Skip the one-shot startup walk; only react to live changes.",
    )
    agent_parser.add_argument(
        "--debounce-ms",
        type=int,
        default=800,
        help="Debounce window for filesystem events (default: 800 ms).",
    )
    agent_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Extra path prefix to skip (repeatable).",
    )

    # --- doctor ---
    subparsers.add_parser("doctor", help="Check environment health")

    # --- rotate-master-key (Wave 2 W2-4) ---
    rotate_parser = subparsers.add_parser(
        "rotate-master-key",
        help="Rotate the master encryption key for providers.db",
    )
    rotate_parser.add_argument(
        "--db",
        default=None,
        help="Path to providers.db (default: ~/.kiro/codebase-mcp/providers.db).",
    )
    rotate_parser.add_argument(
        "--key",
        default=None,
        help="Path to providers.key (default: ~/.kiro/codebase-mcp/providers.key).",
    )
    rotate_parser.add_argument(
        "--new-key",
        default=None,
        help="Optional Fernet-shaped key bytes (base64). When omitted a "
        "fresh key is generated.",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Dispatch to command handlers
    if args.command == "init":
        from omnicode_adapters.cli.commands.init_cmd import run
        run()
    elif args.command == "index":
        from omnicode_adapters.cli.commands.index_cmd import run
        run(
            force=args.force,
            background=args.background,
            scope=args.scope,
            status=args.status,
            backend_url=args.backend_url,
            port=args.port,
            workspace=args.workspace,
            workspace_id=args.workspace_id,
        )
    elif args.command == "status":
        from omnicode_adapters.cli.commands.status_cmd import run
        run()
    elif args.command == "mcp":
        from omnicode_adapters.cli.commands.mcp_cmd import run
        run(
            transport=args.transport,
            host=args.host,
            port=args.port,
            mount_path=args.mount_path,
            auth=args.auth,
            backend_url=args.backend_url,
            backend_token=args.backend_token,
            workspace=args.workspace,
            workspace_id=args.workspace_id,
            executor=args.executor,
            sync_mode=args.sync_mode,
            agent=args.agent,
            llm_mode=args.llm_mode,
            embedding_mode=args.embedding_mode,
        )
    elif args.command == "serve":
        from omnicode_adapters.cli.commands.serve_cmd import run
        run(
            headless=args.headless,
            host=args.host,
            port=args.port,
            reload=args.reload,
            mode=args.mode,
            state_dir=args.state_dir,
            workspace_store=args.workspace_store,
            content_store=args.content_store,
            materialize_mirror=args.materialize_mirror,
            mirror_readonly=args.mirror_readonly,
        )
    elif args.command == "dev":
        from omnicode_adapters.cli.commands.serve_cmd import run
        run(headless=False, host=args.host, port=args.port, reload=True)
    elif args.command == "agent":
        from omnicode_adapters.cli.commands.agent_cmd import run as run_agent_cmd
        run_agent_cmd(
            remote=args.remote,
            token=args.token,
            workspace=args.workspace,
            workspace_id=args.workspace_id,
            initial_sync=not args.no_initial_sync,
            debounce_ms=args.debounce_ms,
            exclude=tuple(args.exclude or ()),
        )
    elif args.command == "doctor":
        from omnicode_adapters.cli.commands.doctor_cmd import run
        run()
    elif args.command == "rotate-master-key":
        from omnicode_adapters.cli.commands.rotate_cmd import run as run_rotate
        run_rotate(db_path=args.db, key_path=args.key, new_key=args.new_key)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
