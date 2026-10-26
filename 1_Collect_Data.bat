@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  1_Collect_Data.bat - candle data collector launcher
rem
rem  All Korean menu text and input live in collect_candles.py (--menu).
rem  Keep this file ASCII-only. Under chcp 65001, cmd mis-tracks line
rem  positions in UTF-8 batch files and runs Korean text as commands
rem  (depends on goto/choice/set /p and even stdin state).
rem ============================================================

cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto :novenv

"%PY%" -X utf8 collect_candles.py --menu
echo.
pause
exit /b 0

:novenv
echo [ERROR] .venv\Scripts\python.exe not found.
echo         Create the venv and run: pip install -r requirements.txt
pause
exit /b 1
