# scripts/

Small helper scripts for local development, testing, and benchmark runs.

## Windows

| Script | Purpose |
|---|---|
| `run.bat` | Start the web/API backend at `http://127.0.0.1:6789/`. |
| `run-dev.bat` | Start the backend with auto-reload. |
| `test.bat` | Run the test suite. |
| `lint.bat` | Run `ruff` checks without modifying files. |

## macOS / Linux

```bash
chmod +x scripts/*.sh
./scripts/run.sh
./scripts/run-dev.sh
./scripts/test.sh
./scripts/lint.sh
```

## Environment

The shell wrappers default to the `omnicode-env` conda environment and port
`6789`. Override them when needed:

```cmd
set CONDA_ENV_NAME=my-env
set PORT=7000
scripts\run.bat
```

```bash
CONDA_ENV_NAME=my-env PORT=7000 ./scripts/run.sh
```

If you use `venv` instead of conda, activate it and run the Python/CLI commands
directly.

## Large Repo Hybrid Benchmark

Run a clean-room large repository benchmark with a temporary cloud-index
backend:

```powershell
python scripts/benchmark_large_repo_hybrid.py `
  --repo C:/omnicode-sim/benchmark-repos/django `
  --state-dir C:/omnicode-sim/state-bench-django `
  --workspace-id django-cleanroom-bench `
  --reset-state
```

This verifies initial sync, snapshot storage, exact symbol/text search,
`index_readiness.v1`, and strict semantic stale handling. Add
`--semantic-bootstrap` only when you want to force a full semantic bootstrap.
