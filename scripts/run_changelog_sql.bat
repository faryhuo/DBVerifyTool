@echo off
REM If double-clicked, relaunch in a persistent cmd window so it never closes on its own
echo %cmdcmdline% | find /i "%~nx0" >nul
if not errorlevel 1 if not defined RELAUNCHED (
    set "RELAUNCHED=1"
    cmd /k ""%~f0""
    exit /b
)
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

REM ===== Configuration =====
set "CHANGELOG=changelogs/changelog_ACE_GWP_ID.xml"
set "DB_SERVER=SWCOLDBOS0B.TECH.OCBC.NET,1440"
set "DB_NAME=CLMS_GWP_ID"
REM Authentication: 1 = Windows integrated authentication (default)
REM                 0 = SQL login (also set DB_USER and DB_PASSWORD below)
set "USE_TRUSTED=1"
set "DB_USER=your_user"
set "DB_PASSWORD=your_password"
REM On failure: 0 = stop immediately, 1 = continue with the next file
set "CONTINUE_ON_ERROR=0"
REM =========================

if not exist "%CHANGELOG%" (
    echo Changelog not found: %CHANGELOG%
    exit /b 1
)

where sqlcmd >nul 2>nul
if errorlevel 1 (
    echo sqlcmd not found. Please install the SQL Server command line utilities.
    exit /b 1
)

set "LIST=%TEMP%\sqlfiles_%RANDOM%.txt"
set "LOGFILE=run_changelog_sql.log"

REM Extract sqlFile paths in order, resolved relative to the changelog directory
powershell -NoProfile -Command ^
  "$cl=(Resolve-Path '%CHANGELOG%').Path; $dir=Split-Path $cl;" ^
  "[xml]$x=Get-Content -Raw -Encoding UTF8 $cl;" ^
  "$p=@($x.SelectNodes('//*[local-name()=''changeSet'']/*[local-name()=''sqlFile'']') | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $dir $_.path)) });" ^
  "[IO.File]::WriteAllLines('%LIST%',[string[]]$p,(New-Object Text.UTF8Encoding $false))"
if errorlevel 1 (
    echo Failed to parse the changelog.
    exit /b 1
)

set /a TOTAL=0
for /f "usebackq delims=" %%F in ("%LIST%") do set /a TOTAL+=1
echo Found %TOTAL% SQL files. Log file: %LOGFILE%
echo Server: %DB_SERVER%  Database: %DB_NAME%
echo.

if "%USE_TRUSTED%"=="1" (
    set "AUTH=-E"
    echo Authentication: Windows integrated
) else (
    set "AUTH=-U %DB_USER% -P %DB_PASSWORD%"
    echo Authentication: SQL login ^(%DB_USER%^)
)
echo.

set /a N=0, OK=0, FAIL=0
for /f "usebackq delims=" %%F in ("%LIST%") do (
    set /a N+=1
    if not exist "%%F" (
        echo [!N!/%TOTAL%] MISSING  %%F
        set /a FAIL+=1
        if "%CONTINUE_ON_ERROR%"=="0" goto :done
    ) else (
        echo ==== %%F >> "%LOGFILE%"
        sqlcmd -S "%DB_SERVER%" -d "%DB_NAME%" !AUTH! -f 65001 -b -i "%%F" >> "%LOGFILE%" 2>&1
        if errorlevel 1 (
            echo [!N!/%TOTAL%] FAILED   %%~nxF
            set /a FAIL+=1
            if "%CONTINUE_ON_ERROR%"=="0" goto :done
        ) else (
            echo [!N!/%TOTAL%] OK       %%~nxF
            set /a OK+=1
        )
    )
)

:done
del "%LIST%" >nul 2>nul
echo.
echo Finished: !OK! succeeded, !FAIL! failed. See %LOGFILE% for details.
if !FAIL! gtr 0 exit /b 1
exit /b 0
