@echo off
REM ============================================================================
REM  OmniCode-MCP — Web backend with auto-reload (development mode)
REM  uvicorn watches source files and restarts on save.
REM ============================================================================
SETLOCAL ENABLEDELAYEDEXPANSION

SET "PROJECT_ROOT=%~dp0.."
PUSHD "%PROJECT_ROOT%" || (
    echo Could not enter project root.
    pause
    exit /b 1
)

IF "%CONDA_ENV_NAME%"=="" SET "CONDA_ENV_NAME=omnicode-env"
IF "%PORT%"=="" SET "PORT=6789"

echo.
echo ============================================================================
echo  OmniCode-MCP backend (DEV - auto-reload)
echo    project : %CD%
echo    env     : %CONDA_ENV_NAME%
echo    port    : %PORT%
echo    URL     : http://127.0.0.1:%PORT%/
echo ============================================================================
echo.

conda run --no-capture-output -n %CONDA_ENV_NAME% uvicorn main:app --port %PORT% --reload

POPD
ENDLOCAL
pause
