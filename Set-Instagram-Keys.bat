@echo off
rem Connects @aisidekicks: asks for the Instagram token and app secret, checks them with Meta, saves them to .env, restarts the assistant.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo JARVIS isn't set up yet. Double-click Setup.bat first.
  pause
  exit /b
)
".venv\Scripts\python.exe" set_instagram_keys.py
if errorlevel 1 (
  echo.
  pause
  exit /b
)
call "%~dp0Start-Sidekicks.bat"
