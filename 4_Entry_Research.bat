@echo off
@chcp 65001 >nul
setlocal

:: ============================================================
::  4단계 - 진입 전략 후보 검증 (연구용)
::  파라미터가 아니라 "진입 조건 자체에 우위가 있는가"를 본다.
::  IS(2026-07-01 이전)에서 고르고 OOS(이후)에서 확인한다.
::  config.py 와 실매매 코드는 건드리지 않는다.
:: ============================================================

cd /d "%~dp0"

set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" -X utf8 entry_research.py --clean-only

echo.
pause
