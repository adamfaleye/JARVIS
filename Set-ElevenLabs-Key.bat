@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo JARVIS isn't set up yet. Double-click Setup.bat first.
  pause
  exit /b
)
".venv\Scripts\python.exe" set_elevenlabs_key.py
echo.
pause
