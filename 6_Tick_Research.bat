@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  6_Tick_Research.bat - entry research on recorded tick data
rem
rem  tick_research.py: tests entry candidates built from
rem  trade strength, buy/sell flow and order book (tick_data\)
rem  with IS/OOS split by date and control groups.
rem  Results are only indicative with fewer than 20 recorded days.
rem
rem  Keep this file ASCII-only (cmd UTF-8 batch bug).
rem ============================================================

cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -X utf8 tick_research.py

echo.
pause
