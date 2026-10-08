@echo off
REM If double-clicked, relaunch in a persistent cmd window so it never closes on its own
echo %cmdcmdline% | find /i "%~nx0" >nul
if not errorlevel 1 if not defined RELAUNCHED (
    set "RELAUNCHED=1"
    cmd /k ""%~f0" %*"
    exit /b
)
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

REM ===== Configuration =====
set "CHANGELOG=changelogs/changelog_ACE_GWP_ID.xml"
REM 1 = also compile every batch with sqlcmd under SET NOEXEC ON (nothing is executed), 0 = static only
set "COMPILE=0"
REM 1 = open the HTML report when finished
set "OPEN_REPORT=1"
REM =========================

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3 not found. Install Python 3.8+ or add it to PATH.
    exit /b 2
)

if not exist "%CHANGELOG%" (
    echo Changelog not found: %CHANGELOG%
    exit /b 2
)

REM The report is overwritten on every run (a timestamped copy is not needed for local checks)
set "OUT=reports\db_verification_report.html"

set "EXTRA="
if "%COMPILE%"=="1" set "EXTRA=--compile"

%PY% "%~dp0ace_db_verify.py" --changelog "%CHANGELOG%" --out "%OUT%" %EXTRA% %*
set "RC=%ERRORLEVEL%"

if "%OPEN_REPORT%"=="1" if exist "%OUT%" start "" "%OUT%"
echo.
if "%RC%"=="0" (echo Verification passed.) else if "%RC%"=="1" (echo Verification found CRITICAL issues - see the report.) else (echo Verification tool error.)
exit /b %RC%
