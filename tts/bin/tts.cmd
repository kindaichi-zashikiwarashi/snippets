@echo off
setlocal
set "SCRIPT_DIR=%~dp0.."
if not defined SBV2_VENV_PYTHON set "SBV2_VENV_PYTHON=C:\tmp\sbv2_venv\Scripts\python.exe"
"%SBV2_VENV_PYTHON%" "%SCRIPT_DIR%\tts.py" %*
