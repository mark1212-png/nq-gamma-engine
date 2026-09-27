@echo off
cd /d "%~dp0"
if not exist "Claude outputs" mkdir "Claude outputs"
echo [%date% %time%] mm_probe estimate > "Claude outputs\mm_probe-run.log"
python mm_probe.py --day 2026-09-04 >> "Claude outputs\mm_probe-run.log" 2>&1
echo [%date% %time%] exit %errorlevel% >> "Claude outputs\mm_probe-run.log"
