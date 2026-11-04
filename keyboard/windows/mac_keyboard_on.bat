@echo off
rem Start the mac_keyboard hook (turn remapping ON)
rem Remove the disabled flag, then start the task
if exist "%APPDATA%\MacKeyboard\disabled.flag" del "%APPDATA%\MacKeyboard\disabled.flag"
tasklist /fi "imagename eq powershell.exe" /v /fo csv 2>nul | findstr /i "MacKeyboard\\hook.ps1" >nul
if %errorlevel%==0 (
    echo Hook is already running.
) else (
    schtasks /run /tn "MacKeyboardHook" >nul
    echo Hook started.
)
