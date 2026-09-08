@echo off
@chcp 65001 >nul
setlocal

:: ============================================================
::  과거 분봉 수집 (백테스트 데이터)
::
::  !! 반드시 main.py 를 종료한 뒤 실행하세요 !!
::     키움은 PC당 세션 1개만 허용하고, TR 제한도 계좌 단위입니다.
::     동시에 돌리면 자동매매의 스캔/손절 주문이 밀립니다.
:: ============================================================

set PY32=C:\Users\user\AppData\Local\Programs\Python\Python311-32\python.exe
if not exist "%PY32%" (
    set PY32=C:\Users\user\AppData\Local\Programs\Python\Python314-32\python.exe
)
if not exist "%PY32%" (
    echo [오류] 32비트 파이썬을 찾을 수 없습니다.
    echo        키움 OCX는 32비트에서만 동작합니다.
    pause
    exit /b 1
)

cd /d "%~dp0"

echo ============================================================
echo   분봉 데이터 수집
echo ============================================================
echo.
echo   [확인] main.py 가 종료되어 있습니까?
echo          실행 중이면 자동매매가 방해받습니다.
echo.
choice /C YN /M "main.py 를 종료했습니다. 계속할까요"
if errorlevel 2 goto :cancel

echo.
echo ============================================================
echo   수집 범위 선택
echo ============================================================
echo.
echo   [1] 대량 수집 - 코스닥 200종목 x 약 84영업일   (3~4시간)
echo       백테스트 거래 100건 목표. 처음이면 이걸 권장.
echo.
echo   [2] 이어받기 - 위와 동일하되 이미 받은 종목은 건너뜀
echo       1번이 중간에 끊겼을 때 사용.
echo.
echo   [3] 빠른 수집 - 조건검색식 종목만            (약 20분)
echo       오늘 급등주만. 종목이 적어 표본은 부족합니다.
echo.
choice /C 123 /M "선택"

if errorlevel 3 goto :cond
if errorlevel 2 goto :resume
goto :bulk

:bulk
echo.
echo [대량 수집] 코스닥 200종목 / 12페이지
echo  * 키움 로그인 창이 뜨면 로그인하세요.
echo  * 중간에 끊어도 그때까지 모은 건 저장됩니다 ([2]로 이어받기).
echo.
"%PY32%" collect_candles.py --market kosdaq --limit 200 --pages 12
goto :done

:resume
echo.
echo [이어받기] 이미 받은 종목은 건너뜁니다
echo.
"%PY32%" collect_candles.py --market kosdaq --limit 200 --pages 12 --skip-existing
goto :done

:cond
echo.
echo [빠른 수집] 조건검색식 종목
echo.
"%PY32%" collect_candles.py
goto :done

:cancel
echo.
echo 취소했습니다. main.py 를 먼저 종료하세요.
goto :end

:done
echo.
echo ============================================================
echo   수집 완료 - 2_Run_Analysis.bat 을 실행하세요
echo ============================================================

:end
echo.
pause
