@echo off
rem Disable the mac_keyboard hook (laptop keyboard back to default)
rem Creates a flag so auto-restart won't bring it back
echo. > "%APPDATA%\MacKeyboard\disabled.flag"
powershell -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe' OR Name='wscript.exe'\" | Where-Object { $_.CommandLine -like '*MacKeyboard*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Hook stopped. Keyboard back to default.
