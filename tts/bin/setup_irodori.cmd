@echo off
REM Setup Irodori-TTS server on Windows.
REM Prereqs: git, uv (auto-installed below), NVIDIA driver >=553 for GPU.
REM First run downloads ~4GB model from HuggingFace. VRAM ~3.9GB.
setlocal

if "%IRODORI_DIR%"=="" set IRODORI_DIR=%USERPROFILE%\ai-tools\Irodori-TTS
set REPO=https://github.com/Aratako/Irodori-TTS.git
set HERE=%~dp0..

echo == Irodori setup ==
echo   Install dir: %IRODORI_DIR%

REM 1. uv
where uv >nul 2>nul
if errorlevel 1 (
  echo [1/4] Installing uv...
  powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
  set PATH=%USERPROFILE%\.local\bin;%PATH%
)
where uv >nul 2>nul
if errorlevel 1 (
  echo ERROR: uv install failed. See https://docs.astral.sh/uv/
  exit /b 1
)
echo [1/4] uv OK

REM 2. clone
if exist "%IRODORI_DIR%\.git" (
  echo [2/4] Already cloned, skipping
) else (
  echo [2/4] Cloning...
  git clone %REPO% "%IRODORI_DIR%"
)

REM 3. uv sync
echo [3/4] uv sync (may take several minutes for torch etc.)...
pushd "%IRODORI_DIR%"
uv sync
popd

REM 4. place server + reference voices
echo [4/4] Placing irodori_server.py and voices/...
copy /Y "%HERE%\irodori_server.py" "%IRODORI_DIR%\irodori_server.py"
if not exist "%IRODORI_DIR%\voices" mkdir "%IRODORI_DIR%\voices"
copy /Y "%HERE%\voices\*.wav" "%IRODORI_DIR%\voices\" 2>nul

echo.
echo == Done ==
echo Start server (first run downloads model ~4GB):
echo   cd /d %IRODORI_DIR%
echo   set HF_HUB_DISABLE_XET=1
echo   uv run --no-sync python irodori_server.py --port 5001
echo.
echo Check: curl http://127.0.0.1:5001/health
echo Then:  clip_tts -v calm
endlocal
