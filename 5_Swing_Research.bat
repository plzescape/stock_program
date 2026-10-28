@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  5_Swing_Research.bat - daily-bar swing entry research
rem
rem  1) collect_daily.py : download daily bars (Kiwoom REST ka10081)
rem     into daily_data\  (resumable, skips files fetched today)
rem  2) swing_research.py: IS/OOS test of swing entry candidates
rem
rem  Do not run during market hours while main.py is running:
rem  both share the same app key rate limit.
rem  Keep this file ASCII-only (cmd UTF-8 batch bug).
rem ============================================================

cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

choice /C YN /M "Download/refresh daily bars first (about 1.5 hours for all stocks)"
if errorlevel 2 goto :research

"%PY%" -X utf8 collect_daily.py --years 3

:research
"%PY%" -X utf8 swing_research.py

echo.
pause
