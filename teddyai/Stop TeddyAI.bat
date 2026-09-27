@echo off
:: Stop TeddyAI -- double-click. Closes the services and their windows, nothing else:
:: teddy.py, live_feed.py and teddychart, matched by what they run rather than by port,
:: plus the consoles the launcher opened for them. The Stop button on the command
:: center's appbar runs this same script. Nothing is written on the way down.
cd /d "%~dp0"
if not exist "%~dp0stop_teddy.ps1" (
  echo   stop_teddy.ps1 is not in this folder -- nothing to stop.
  echo.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_teddy.ps1"
