@echo off
cd /d "%~dp0"
set LOG=Claude outputs\backtest_opra-run.log
echo [%date% %time%] backtest_opra selftest > "%LOG%"
python backtest_opra.py --selftest >> "%LOG%" 2>&1
echo [%date% %time%] bars (cached check, no fetch expected) >> "%LOG%"
python backtest_opra.py --bars --start 2024-09-05 --end 2026-09-03 >> "%LOG%" 2>&1
echo [%date% %time%] score >> "%LOG%"
python backtest_opra.py --score --start 2024-09-03 --end 2026-09-03 >> "%LOG%" 2>&1
echo [%date% %time%] exit %errorlevel% >> "%LOG%"
