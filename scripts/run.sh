#!/usr/bin/env bash
# =============================================================================
#  OmniCode-MCP — start the FastAPI Web backend
#  Usage:   ./scripts/run.sh
#  Override env name:    CONDA_ENV_NAME=my-env ./scripts/run.sh
#  Override port:        PORT=7000 ./scripts/run.sh
# =============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

CONDA_ENV_NAME="${CONDA_ENV_NAME:-omnicode-env}"
PORT="${PORT:-6789}"

cat <<EOF

================================================================================
 OmniCode-MCP backend
   project : $PROJECT_ROOT
   env     : $CONDA_ENV_NAME
   port    : $PORT
   URL     : http://127.0.0.1:$PORT/
================================================================================

EOF

conda run --no-capture-output -n "$CONDA_ENV_NAME" \
    uvicorn main:app --port "$PORT"
