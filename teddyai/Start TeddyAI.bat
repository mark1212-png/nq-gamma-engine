@echo off
:: Start TeddyAI -- double-click. Brings up the browser stack in the right order and
:: opens the command center: teddy.py (:5137), live_feed.py (bar cache), teddychart
:: (:5138), then http://127.0.0.1:5137/cc, which runs the pipeline on open. Each
:: service gets its own console titled "TeddyAI - <name>". Safe to double-click again:
:: a port that already answers is left alone. Warns instead of failing when
:: DATABENTO_API_KEY is missing. For the desktop window instead of four consoles and
:: a browser tab, use "Start TeddyAI (App).bat".
:: From a terminal the switches pass straight through:
::     "Start TeddyAI.bat" -Sim -NoChart -NoFeed -NoBrowser
cd /d "%~dp0"
if not exist "%~dp0start_teddy.ps1" (
  echo   start_teddy.ps1 is not in this folder -- nothing to start.
  echo.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_teddy.ps1" %*
