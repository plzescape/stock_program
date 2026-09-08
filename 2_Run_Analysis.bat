@echo off
@chcp 65001 >nul
setlocal

:: ============================================================
::  2단계 - 통합 분석
::  필터 병목 -> 백테스트 -> 파라미터 권장을 한 번에 실행합니다.
::  config.py 는 건드리지 않습니다 (반영은 3_Apply_Config.bat).
:: ============================================================

set PY=C:\Users\user\AppData\Local\Programs\Python\Python314\python.exe
if not exist "%PY%" set PY=python

cd /d "%~dp0"

"%PY%" -X utf8 run_analysis.py

echo.
pause
