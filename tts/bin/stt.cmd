@echo off
setlocal
set "SCRIPT_DIR=%~dp0.."

rem Pick python: SBV2_VENV_PYTHON > old Windows venv > py / python on PATH
set "PY=%SBV2_VENV_PYTHON%"
if not defined PY if exist "C:\tmp\sbv2_venv\Scripts\python.exe" set "PY=C:\tmp\sbv2_venv\Scripts\python.exe"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (
    echo ERROR: python not found. 1>&2
    echo   Set SBV2_VENV_PYTHON to python.exe, or put python on PATH. 1>&2
    exit /b 1
)

"%PY%" "%SCRIPT_DIR%\stt.py" %*
