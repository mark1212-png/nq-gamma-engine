@echo off
cd /d "%~dp0"
echo [%date% %time%] mm_calib --reuse > "Claude outputs\mm_calib-run.log"
python mm_calib.py --reuse >> "Claude outputs\mm_calib-run.log" 2>&1
echo [%date% %time%] exit %errorlevel% >> "Claude outputs\mm_calib-run.log"
