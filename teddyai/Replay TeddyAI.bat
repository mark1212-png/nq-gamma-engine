@echo off
:: Replay TeddyAI -- re-run a recorded day in "real time": that day's sheet and plan on the page,
:: teddychart replaying its tape, the command center's tracker grading the setups print by print.
::   Replay TeddyAI.bat                      Friday 2026-09-04 from 06:20 PT at 5x
::   Replay TeddyAI.bat 2026-09-04 06:20 1   day, from-time (PT), speed
:: Today's sheet is put back when you press Enter in this window.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0replay_teddy.ps1" %*
