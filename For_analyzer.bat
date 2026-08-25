@echo off
@chcp 65001
setlocal enabledelayedexpansion

:: ===== 1. 오늘 날짜 가져오기 =====
for /f "tokens=1-3 delims=.-/ " %%a in ("%date%") do (
    set yy=%%a
    set mm=%%b
    set dd=%%c
)

:: Windows 날짜 형식이 다를 수 있으므로 보정 (한국형 yyyy-mm-dd 기준)
if "%mm%"=="" (
    for /f %%a in ('powershell -command "Get-Date -Format yy"') do set yy=%%a
    for /f %%a in ('powershell -command "Get-Date -Format MM"') do set mm=%%a
    for /f %%a in ('powershell -command "Get-Date -Format dd"') do set dd=%%a
)

:: yyyy -> yy로 자르기
set shortyy=%yy:~-2%

:: ===== 2. 폴더 및 파일명 설정 =====
set REPORT_FOLDER=Report_%shortyy%%mm%%dd%
set REPORT_NAME=%shortyy%_%mm%_%dd%-Automated Trading Performance Report.xlsx

:: ===== 3. 폴더 생성 =====
if not exist "%REPORT_FOLDER%" (
    mkdir "%REPORT_FOLDER%"
)

:: ===== 4. 리포트 생성 =====
"C:\Users\user\AppData\Local\Programs\Python\Python314\python.exe" generate_report.py logs\trade.log "%REPORT_NAME%"

:: ===== 4-1. 차트 분석 HTML 생성 =====
echo 종목별 차트 분석 중...
"C:\Users\user\AppData\Local\Programs\Python\Python314\python.exe" analyze_trades.py logs "%REPORT_FOLDER%"
echo 차트 분석 완료

:: ===== 5. 리포트 이동 =====
move "%REPORT_NAME%" "%REPORT_FOLDER%\%REPORT_NAME%"

:: ===== 6. 로그 파일 이동 =====
if exist logs\trade.log move logs\trade.log "%REPORT_FOLDER%\trade.log"
if exist logs\signal.log move logs\signal.log "%REPORT_FOLDER%\signal.log"
if exist logs\system.log move logs\system.log "%REPORT_FOLDER%\system.log"
if exist logs\discord.log move logs\discord.log "%REPORT_FOLDER%\discord.log"

type nul > logs\trade.log
type nul > logs\signal.log
type nul > logs\system.log
type nul > logs\discord.log


echo ==========================
echo 리포트 생성 및 로그 정리 완료
echo ==========================

pause