@echo off
REM Windows launcher — start backend, then open the UI in default browser.
setlocal
cd /d "%~dp0..\backend"

REM Pick camera index (0 = first webcam). Override: set MM_CAM=1 before running.
if "%MM_CAM%"=="" set MM_CAM=1

REM Fire up the browser after a short delay so the server is ready.
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8080/"

python app.py
endlocal
