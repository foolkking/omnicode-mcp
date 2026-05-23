@echo off
REM ============================================================================
REM  OmniCode-MCP — full test suite (unit + integration)
REM  Expected:267 passed, 1 skipped, ~90 s
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
echo  Running full test suite (env: %CONDA_ENV_NAME%)
echo ============================================================================
echo.

conda run --no-capture-output -n %CONDA_ENV_NAME% python -m pytest tests -q --tb=short

POPD
ENDLOCAL
pause
