@echo off
cd /d "%~dp0"
echo [%date% %time%] mm_levels > "Claude outputs\mm_levels-run.log"
python mm_levels.py >> "Claude outputs\mm_levels-run.log" 2>&1
echo [%date% %time%] exit %errorlevel% >> "Claude outputs\mm_levels-run.log"
