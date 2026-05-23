@echo off
REM ============================================================================
REM  OmniCode-MCP — ruff lint
REM  Important:never run with --fix on tests/ (history reason:auto-fix once
REM  deleted the whole directory). Use only `check`.
REM ============================================================================
SETLOCAL

SET "PROJECT_ROOT=%~dp0.."
PUSHD "%PROJECT_ROOT%" || (
    echo Could not enter project root.
    pause
    exit /b 1
)

IF "%CONDA_ENV_NAME%"=="" SET "CONDA_ENV_NAME=omnicode-env"

echo.
echo ============================================================================
echo  Linting omnicode/ api/ core/ tests/  (env: %CONDA_ENV_NAME%)
echo ============================================================================
echo.

conda run --no-capture-output -n %CONDA_ENV_NAME% ruff check omnicode api core tests

POPD
ENDLOCAL
pause
