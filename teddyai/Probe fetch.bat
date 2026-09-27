@echo off
cd /d "%~dp0"
echo [%date% %time%] mm_probe fetch+replay > "Claude outputs\mm_probe-run.log"
python mm_probe.py --day 2026-09-04 --fetch >> "Claude outputs\mm_probe-run.log" 2>&1
echo [%date% %time%] exit %errorlevel% >> "Claude outputs\mm_probe-run.log"
