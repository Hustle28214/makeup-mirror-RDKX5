@echo off
REM Windows launcher — makeup evenness mode.
setlocal
cd /d "%~dp0..\backend"

if "%MM_CAM%"=="" set MM_CAM=1
set MM_MODE=makeup

start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8080/"

python app.py
endlocal
