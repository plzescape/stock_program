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

set "PY32=%~dp0.venv\Scripts\python.exe"
if not exist "%PY32%" goto :novenv

"%PY32%" collect_candles.py --menu
echo.
pause
exit /b 0

:novenv
echo [ERROR] .venv\Scripts\python.exe not found.
echo         Kiwoom OpenAPI OCX needs the 32-bit Python virtual env.
pause
exit /b 1
