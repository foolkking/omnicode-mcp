#!/usr/bin/env bash
# =============================================================================
#  OmniCode-MCP — full test suite
#  Expected:267 passed, 1 skipped, ~90 s
# =============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

CONDA_ENV_NAME="${CONDA_ENV_NAME:-omnicode-env}"

echo
echo "================================================================================"
echo " Running full test suite (env: $CONDA_ENV_NAME)"
echo "================================================================================"
echo

conda run --no-capture-output -n "$CONDA_ENV_NAME" \
    python -m pytest tests -q --tb=short
