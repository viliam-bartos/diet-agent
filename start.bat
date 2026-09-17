@echo off
setlocal
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

echo Web dashboard i Discord bot se spousti v tomto okne.
echo Ukonceni: Ctrl+C, pak pripadne potvrdit Y.
echo.

".venv\Scripts\python.exe" -m nutrition_agent.cli --run-all --open-browser

echo.
echo Aplikace byla ukoncena.
echo.
pause
exit /b 0

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
