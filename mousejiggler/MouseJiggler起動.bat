@echo off
REM このファイルをダブルクリックするとターミナルが開き、マウスジグラーが起動します。
REM 止めるときは、開いたターミナルで Ctrl + C を押してください。
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0MouseJiggler.ps1"
echo.
echo 終了しました。このウィンドウは閉じて構いません。
pause
