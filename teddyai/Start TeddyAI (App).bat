@echo off
:: Start TeddyAI (App) -- double-click. The same three services in the same order as
:: "Start TeddyAI.bat", but in one native window instead of four consoles and a
:: browser tab: tray icon, single instance (launching again raises the window that is
:: open), Win+Alt+T / +C / +G for terminal, chart and ladder, and tear-out windows for
:: the chart and the gamma ladder. See teddy_app.py's header.
:: From a terminal the switches pass straight through:
::     "Start TeddyAI (App).bat" --sim --no-chart --attach
cd /d "%~dp0"
if not exist "%~dp0teddy_app.py" (
  echo   teddy_app.py is not in this folder -- use "Start TeddyAI.bat" instead.
  echo.
  pause
  exit /b 1
)
:: pywebview is what draws the window. Say so here rather than let python traceback
:: and the console vanish with it.
python -c "import webview" 1>nul 2>nul
if errorlevel 1 (
  echo   pywebview is not installed, so the desktop window cannot open.
  echo     pip install pywebview pystray pillow
  echo   Or use "Start TeddyAI.bat" for the browser stack.
  echo.
  pause
  exit /b 1
)
python teddy_app.py %*
if errorlevel 1 (
  echo.
  echo   teddy_app.py exited with an error -- the message is above.
  pause
)
