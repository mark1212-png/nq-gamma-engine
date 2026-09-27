@echo off
cd /d "%~dp0"
if "%DATABENTO_API_KEY%"=="" echo DATABENTO_API_KEY is not set. Set it first, or use run-sim.bat. && pause && exit /b 1
start "" http://127.0.0.1:5138/
python server.py %*
pause
