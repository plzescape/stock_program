@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  For_analyzer.bat - daily report builder launcher
rem
rem  Date handling, report building and log moving live in
rem  make_report.py. Reports go to reports\Report_YYMMDD\.
rem  Keep this file ASCII-only: under chcp 65001 cmd mis-tracks
rem  line positions in UTF-8 batch files and runs Korean text
rem  as commands.
rem ============================================================

cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -X utf8 make_report.py %1

echo.
pause
