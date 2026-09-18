@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Nutrition Agent

echo ==========================================================
echo   Nutrition Agent
echo ==========================================================
echo.

if not exist ".venv\Scripts\python.exe" goto :no_venv
if not exist ".env" goto :no_env

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, discord, google.genai" 2>nul
if errorlevel 1 goto :no_deps

rem Port from .env, defaulting to 8000.
set "WEB_PORT=8000"
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="WEB_PORT" set "WEB_PORT=%%~b"
)

rem A leftover server still holds the port. If we start anyway, uvicorn fails to bind
rem and the browser keeps talking to the outdated process, which looks like the app
rem silently ignoring new code.
call :stop_existing
if errorlevel 1 (
    echo CHYBA: Port %WEB_PORT% se nepodarilo uvolnit.
    echo.
    echo Zavri ostatni okna s Nutrition Agent a spust start.bat znovu.
    echo Pripadne zkontroluj, kdo port drzi:
    echo   netstat -ano ^| findstr :%WEB_PORT%
    echo.
    pause
    exit /b 1
)

echo Web dashboard i Discord bot se spousti v tomto okne.
echo Ukonceni: Ctrl+C, pak pripadne potvrdit Y.
echo.

".venv\Scripts\python.exe" -m nutrition_agent.cli --run-all --open-browser

echo.
echo Aplikace byla ukoncena.
echo.
pause
exit /b 0

:stop_existing
set "FOUND=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%WEB_PORT% .*LISTENING"') do (
    if not "%%p"=="0" (
        set "FOUND=1"
        echo Nalezena predchozi instance na portu %WEB_PORT% ^(PID %%p^), ukoncuji ji.
        taskkill /F /T /PID %%p >nul 2>&1
    )
)
if "%FOUND%"=="0" exit /b 0

rem taskkill returns before the socket is released, so wait for the port to free up.
set /a TRIES=0
:wait_port
netstat -ano | findstr /r /c:":%WEB_PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 exit /b 0
set /a TRIES+=1
if %TRIES% GEQ 15 exit /b 1
timeout /t 1 /nobreak >nul
goto :wait_port

:no_venv
echo CHYBA: Chybi virtualni prostredi .venv
echo.
echo Spustte v tomto adresari:
echo   python -m venv .venv
echo   .venv\Scripts\pip install -e ".[dev]"
echo.
pause
exit /b 1

:no_env
echo CHYBA: Chybi soubor .env
echo.
echo Vytvorte ho prikazem:
echo   copy .env.example .env
echo a doplnte GEMINI_API_KEY a DISCORD_BOT_TOKEN.
echo.
pause
exit /b 1

:no_deps
echo CHYBA: V prostredi .venv chybi nektera zavislost.
echo.
echo Doplnte je prikazem:
echo   .venv\Scripts\pip install -e ".[dev]"
echo.
pause
exit /b 1
