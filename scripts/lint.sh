#!/usr/bin/env bash
# =============================================================================
#  OmniCode-MCP — ruff lint
#  Important:never run with --fix on tests/ (history reason:auto-fix once
#  deleted the whole directory). Use only `check`.
# =============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

CONDA_ENV_NAME="${CONDA_ENV_NAME:-omnicode-env}"

echo
echo "================================================================================"
echo " Linting omnicode/ api/ core/ tests/  (env: $CONDA_ENV_NAME)"
echo "================================================================================"
echo

conda run --no-capture-output -n "$CONDA_ENV_NAME" \
    ruff check omnicode api core tests
