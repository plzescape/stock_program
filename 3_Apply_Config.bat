@echo off
@chcp 65001 >nul
setlocal

:: ============================================================
::  3단계 - 권장 파라미터를 config.py 에 반영
::  거래 100건 미만이면 자동으로 거부합니다.
::  (소표본 최적값 = 과최적화 = 반영하면 오히려 나빠짐)
::  기존 config.py 는 config.py.bak 으로 백업됩니다.
:: ============================================================

set PY=C:\Users\user\AppData\Local\Programs\Python\Python314\python.exe
if not exist "%PY%" set PY=python

cd /d "%~dp0"

echo ============================================================
echo   config.py 에 권장값을 반영합니다
echo   기존 설정은 config.py.bak 으로 백업됩니다
echo ============================================================
echo.
choice /C YN /M "계속하시겠습니까"
if errorlevel 2 goto :cancel

"%PY%" -X utf8 run_analysis.py --apply
goto :done

:cancel
echo 취소했습니다.

:done
echo.
pause
