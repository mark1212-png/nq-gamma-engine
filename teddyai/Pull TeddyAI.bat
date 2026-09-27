@echo off
:: Pull TeddyAI -- double-click. Runs one pull-and-build for whatever session the
:: clock says it is (python autopull.py --job auto): chain from Databento, verify,
:: ladder, build, archive. Nothing else is started; it needs DATABENTO_API_KEY in
:: the environment, and it does nothing outside a job window (see COMMANDS.txt 3).
:: From a terminal, arguments replace the default job and pass straight through:
::     "Pull TeddyAI.bat" --job intraday --hours 2.5
:: The window stays open so the log is readable; the same text is in notes\autopull.log.
cd /d "%~dp0"
if "%~1"=="" (python autopull.py --job auto) else (python autopull.py %*)
echo.
pause
